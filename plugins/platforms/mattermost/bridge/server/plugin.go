package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin"
	"github.com/mattermost/mattermost/server/public/pluginapi"
)

// Bridge is the Mattermost server plugin that relays slash commands and
// interactive actions (buttons/menus/dialogs) from the MM UI to the Hermes
// bot over its outbound WebSocket, so Hermes can stay behind NAT.
type Bridge struct {
	plugin.MattermostPlugin

	client   *pluginapi.Client
	cfg      *configuration
	registry []CommandSpec // commands this plugin currently owns (last-wins)

	// Interactive form state for multi-control posts (buttons + select menus).
	// keyed by post_id → action_id → selected value. A post with a single action
	// is a final choice and is NOT stored here (it is redraw-disabled instead).
	// Guarded by mu. This makes the plugin stateful FOR FORMS ONLY — it never
	// interprets what an agent will do, it just accumulates selections so a
	// multi-control post can redraw (show progress) without wiping other fields.
	mu        sync.Mutex
	formState map[string]map[string]string // post_id -> action_id -> selected
}

// configuration mirrors the server-side plugin settings.
type configuration struct {
	// SharedSecret must match the one kept by the Hermes host so the
	// command-registry REST endpoint (/plugins/<id>/config) is authenticated.
	// Field name must equal the settings_schema key (Mattermost stores the
	// value under "SharedSecret" and LoadPluginConfiguration matches by name).
	SharedSecret string
}

// CommandSpec is a registry entry Hermes pushes to the plugin.
type CommandSpec struct {
	Trigger      string `json:"trigger"`
	Hint         string `json:"hint,omitempty"`
	Description  string `json:"description,omitempty"`
	AutoComplete bool   `json:"autocomplete,omitempty"`
}

// registryPayload is the body of POST /plugins/<id>/config from Hermes.
type registryPayload struct {
	Commands []CommandSpec `json:"commands"`
	Replace  bool          `json:"replace"`
}

const (
	wsExitCommand  = "hermes_bridge_command"
	wsExitInteract = "hermes_bridge_interact"
	wsExitDialog   = "hermes_bridge_dialog"

	// dialogURL is where the MM server will POST the SubmitDialogRequest when the
	// user submits an opened dialog. Must match this plugin's id in plugin.json
	// (the server resolves /plugins/<id>/<path> locally into ServeHTTP).
	dialogURL = "/plugins/hermes-bridge/dialog"
)

func (p *Bridge) OnConfigurationChange() error {
	if p.client == nil {
		p.client = pluginapi.NewClient(p.API, p.Driver)
	}
	var cfg configuration
	if err := p.API.LoadPluginConfiguration(&cfg); err != nil {
		log.Printf("hermes-bridge: load config: %v", err)
		return err
	}
	p.cfg = &cfg
	return nil
}

// ExecuteCommand fires for every slash command this plugin registered. It does
// not interpret the command — it forwards the raw invocation to the Hermes bot
// and returns an async placeholder so the client doesn't hang.
func (p *Bridge) ExecuteCommand(c *plugin.Context, args *model.CommandArgs) (*model.CommandResponse, *model.AppError) {
	cmd := strings.TrimPrefix(args.Command, "/")
	trigger, argsRest, _ := strings.Cut(cmd, " ")

	channelType := ""
	if ch, err := p.API.GetChannel(args.ChannelId); err == nil {
		channelType = string(ch.Type)
	}
	userName := ""
	if u, err := p.API.GetUser(args.UserId); err == nil {
		userName = u.Username
	}

	payload := map[string]any{
		"trigger":      trigger,
		"args":         strings.TrimSpace(argsRest),
		"channel_id":   args.ChannelId,
		"channel_type": channelType,
		"user_id":      args.UserId,
		"user_name":    userName,
		"thread_id":    args.RootId,
		"response_url": "", // async replies go out via the bot's own delivery
	}
	p.API.LogInfo("hermes-bridge ExecuteCommand",
		"trigger", trigger,
		"channel_id", args.ChannelId,
		"channel_type", channelType,
		"target_channel_broadcast", true)
	target := &model.WebsocketBroadcast{ChannelId: args.ChannelId}
	p.API.PublishWebSocketEvent(wsExitCommand, payload, target)

	return &model.CommandResponse{
		ResponseType: model.CommandResponseTypeEphemeral,
		Text:         "Запрос отправлен боту.",
	}, nil
}

// ServeHTTP handles interactive callbacks (buttons/menus/dialogs) delivered
// locally at /plugins/<plugin_id>/suffix and the Hermes command-registry
// endpoint at /plugins/<plugin_id>/config.
func (p *Bridge) ServeHTTP(c *plugin.Context, w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/config":
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		p.handleConfig(w, r)
	case "/interact":
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		p.handleInteract(w, r)
	case "/dialog":
		// Interactive-dialog submission (SubmitDialogRequest) delivered locally.
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		p.handleDialogSubmit(w, r)
	default:
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		writeErr(w, http.StatusNotFound, "unknown plugin endpoint: "+r.URL.Path)
	}
}

func (p *Bridge) handleConfig(w http.ResponseWriter, r *http.Request) {
	auth := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if p.cfg == nil || p.cfg.SharedSecret == "" || auth != p.cfg.SharedSecret {
		writeErr(w, http.StatusUnauthorized, "unauthorized")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var payload registryPayload
	if err := json.Unmarshal(body, &payload); err != nil {
		writeErr(w, http.StatusBadRequest, "bad json: "+err.Error())
		return
	}

	teamID := "" // plugin-registered commands apply regardless of team trigger prefix
	if payload.Replace {
		for _, spec := range p.registry {
			if err := p.API.UnregisterCommand(teamID, spec.Trigger); err != nil {
				writeErr(w, http.StatusInternalServerError, "unregister "+spec.Trigger+": "+err.Error())
				return
			}
		}
		p.registry = nil
	}

	registered := 0
	for _, spec := range payload.Commands {
		if err := p.API.RegisterCommand(registryCommand(spec)); err != nil {
			writeErr(w, http.StatusInternalServerError, "register "+spec.Trigger+": "+err.Error())
			return
		}
		p.registry = append(p.registry, spec)
		registered++
	}
	jsonOk(w, map[string]any{"ok": true, "registered": registered})
}

func (p *Bridge) handleInteract(w http.ResponseWriter, r *http.Request) {
	// Mattermost delivers every button/menu click as a PostActionIntegrationRequest
	// (the same JSON an external integration would get). Decode it and relay a
	// structured WS event to the channel so the Hermes adapter can build a
	// MessageEvent without knowing the MM wire format.
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var req model.PostActionIntegrationRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad json: "+err.Error())
		return
	}

	// Dialog trigger: if the button's integration.context carries a "dialog"
	// schema, this click means "open the form" — not a choice. Open the dialog
	// (trigger_id > server renders the modal) and relay nothing more as a choice;
	// the eventual SubmitDialogRequest arrives separately at /dialog. The plugin
	// stays a dumb relay: the full Dialog schema came from Hermes inside context.
	if dialogRaw, ok := req.Context["dialog"]; ok && dialogRaw != nil {
		p.handleDialogOpen(w, &req, dialogRaw)
		return
	}

	// The action identity lives in the integration context we attached when the
	// post was created (the bot puts {action_id, ...} there). Menus add
	// selected_option. Fall back to a type marker when context is empty.
	actionID, _ := req.Context["action_id"].(string)
	if actionID == "" {
		actionID = req.Type
	}
	selected, _ := req.Context["selected_option"].(string)
	if selected == "" {
		if lbl, _ := req.Context["label"].(string); lbl != "" {
			selected = lbl
		}
	}
	isSubmit := false
	if sb, ok := req.Context["submit"].(bool); ok && sb {
		isSubmit = true
	}

	// Always relay the accumulated form state for this post (all controls), so the
	// agent sees progress across clicks. On a submit action the agent ALSO gets the
	// full submission as a single map (all selected values).
	formState := p.snapshotFormState(req.PostId)
	payload := map[string]any{
		"action_id":       actionID,
		"selected_option": selected,
		"context":         req.Context,
		"user_id":         req.UserId,
		"user_name":       req.UserName,
		"channel_id":      req.ChannelId,
		"channel_name":    req.ChannelName,
		"team_id":         req.TeamId,
		"post_id":         req.PostId,
		"trigger_id":      req.TriggerId,
		"type":            req.Type,
		"data_source":     req.DataSource,
		"form_state":      formState,
	}
	if isSubmit {
		payload["submission"] = formState
	}
	log.Printf("hermes-bridge interact action=%s submit=%t user=%s channel=%s",
		actionID, isSubmit, req.UserId, req.ChannelId)
	p.API.PublishWebSocketEvent(wsExitInteract, payload, &model.WebsocketBroadcast{ChannelId: req.ChannelId})

	// Submit: the form is complete — disable every control (final answer delivered).
	// Normal click: accumulate + redraw by state, keeping every other control live.
	if req.PostId != "" {
		if isSubmit {
			// Complete: show ALL accumulated values and disable every action.
			if updated := p.interactRedrawFinal(req.PostId); updated != nil {
				if err := p.client.Post.UpdatePost(updated); err != nil {
					log.Printf("hermes-bridge interact: update post failed: %v", err)
				}
			}
			return
		}
		single := p.postIsSingleAction(req.PostId)
		if single {
			// Final single choice → disable-after-pick (old behaviour).
			if updated := p.interactRedrawPost(req.PostId, selected); updated != nil {
				if err := p.client.Post.UpdatePost(updated); err != nil {
					log.Printf("hermes-bridge interact: update post failed: %v", err)
				}
			}
		} else {
			// A multi-control form → accumulate + redraw by state (keep all controls).
			if updated := p.interactRedrawStateful(req.PostId, actionID, selected); updated != nil {
				if err := p.client.Post.UpdatePost(updated); err != nil {
					log.Printf("hermes-bridge interact: update post failed: %v", err)
				}
			}
		}
	}
	// MM requires a 200 JSON response or it shows "Action failed to execute".
	jsonOk(w, map[string]any{"ok": true})
}

func (p *Bridge) snapshotFormState(postID string) map[string]string {
	p.mu.Lock()
	defer p.mu.Unlock()
	cp := make(map[string]string, len(p.formState[postID]))
	for k, v := range p.formState[postID] {
		cp[k] = v
	}
	return cp
}

// postIsSingleAction reports whether the post has exactly one total interactive
// action across all attachments. Single => the click is a final choice (safe to
// redraw-disable). Multiple => a multi-control form; the plugin must NOT redraw it.
func (p *Bridge) postIsSingleAction(postID string) bool {
	post, err := p.client.Post.GetPost(postID)
	if err != nil || post == nil {
		return false // can't tell → safer not to redraw
	}
	props := post.GetProps()
	attachments, _ := props["attachments"].([]any)
	total := 0
	for _, a := range attachments {
		am, ok := a.(map[string]any)
		if !ok {
			continue
		}
		acts, ok := am["actions"].([]any)
		if !ok {
			continue
		}
		total += len(acts)
	}
	return total == 1
}

// handleDialogOpen opens an interactive dialog on the clicking user's client. The
// button's integration.context carried a "dialog" schema (posted by Hermes), which
// is the OpenDialogRequest minus trigger_id/url — the server needs trigger_id (we
// have it from the callback) and the local URL where the submit must come back.
// The plugin does not interpret the schema; it decodes it verbatim and relays.
func (p *Bridge) handleDialogOpen(w http.ResponseWriter, req *model.PostActionIntegrationRequest, dialogRaw any) {
	dlgBytes, err := json.Marshal(dialogRaw)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "encode dialog schema: "+err.Error())
		return
	}
	var dlg model.Dialog
	if err := json.Unmarshal(dlgBytes, &dlg); err != nil {
		writeErr(w, http.StatusBadRequest, "bad dialog schema: "+err.Error())
		return
	}
	if req.TriggerId == "" {
		writeErr(w, http.StatusBadRequest, "dialog open requires a trigger_id")
		return
	}
	p.API.LogInfo("hermes-bridge open dialog", "callback_id", dlg.CallbackId,
		"channel_id", req.ChannelId, "user_id", req.UserId)
	appErr := p.API.OpenInteractiveDialog(model.OpenDialogRequest{
		TriggerId: req.TriggerId,
		URL:       dialogURL,
		Dialog:    dlg,
	})
	if appErr != nil {
		log.Printf("hermes-bridge open dialog failed: %v", appErr.Error())
		writeErr(w, http.StatusInternalServerError, "open dialog: "+appErr.Error())
		return
	}
	jsonOk(w, map[string]any{"ok": true})
}

// handleDialogSubmit receives a SubmitDialogRequest (the user submitted/cancelled a
// dialog we opened) and relays it as a structured hermes_bridge_dialog WS event.
// The submission map ({field_name: value}) is opaque to the plugin — Hermes owns
// the dialog schema and interprets the fields.
func (p *Bridge) handleDialogSubmit(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var req model.SubmitDialogRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, http.StatusBadRequest, "bad json: "+err.Error())
		return
	}
	userName := ""
	if u, err := p.API.GetUser(req.UserId); err == nil {
		userName = u.Username
	}
	payload := map[string]any{
		"callback_id": req.CallbackId,
		"state":       req.State,
		"submission":  req.Submission,
		"cancelled":   req.Cancelled,
		"user_id":     req.UserId,
		"user_name":   userName,
		"channel_id":  req.ChannelId,
		"team_id":     req.TeamId,
	}
	p.API.LogInfo("hermes-bridge dialog submit", "callback_id", req.CallbackId,
		"cancel", req.Cancelled, "channel_id", req.ChannelId)
	p.API.PublishWebSocketEvent(wsExitDialog, payload, &model.WebsocketBroadcast{ChannelId: req.ChannelId})
	jsonOk(w, map[string]any{"ok": true})
}

// interactRedrawFinal shows all accumulated form values and clears every action
// — used when the form's Submit button is clicked (the form is complete).
func (p *Bridge) interactRedrawFinal(postID string) *model.Post {
	post, err := p.client.Post.GetPost(postID)
	if err != nil || post == nil {
		return nil
	}
	updated := post.Clone()
	srcProps := post.GetProps()
	props := make(map[string]any, len(srcProps))
	for k, v := range srcProps {
		props[k] = v
	}
	p.mu.Lock()
	sel := make(map[string]string, len(p.formState[postID]))
	for k, v := range p.formState[postID] {
		sel[k] = v
	}
	p.mu.Unlock()

	attachments, _ := props["attachments"].([]any)
	built := make([]any, 0, len(attachments))
	allSel := []string{}
	for _, a := range attachments {
		am, ok := a.(map[string]any)
		if !ok {
			built = append(built, a)
			continue
		}
		cp := make(map[string]any, len(am))
		for k, v := range am {
			cp[k] = v
		}
		delete(cp, "actions") // form complete → no more controls
		base := ""
		if s, ok := cp["text"].(string); ok {
			if i := strings.Index(s, "\n\n**Выбор:**"); i >= 0 {
				base = strings.TrimSpace(s[:i])
			} else {
				base = strings.TrimSpace(s)
			}
		}
		lines := []string{}
		for aid, v := range sel {
			if v == "" {
				continue
			}
			lines = append(lines, "**"+aid+":** "+v)
			allSel = append(allSel, aid+"="+v)
		}
		txt := base
		if len(lines) > 0 {
			txt = base + "\n\n**Выбор:**\n" + strings.Join(lines, "\n")
		}
		cp["text"] = txt
		built = append(built, cp)
	}
	if len(built) > 0 {
		props["attachments"] = built
	}
	props["selected"] = strings.Join(allSel, "; ")
	updated.SetProps(props)
	return updated
}

func (p *Bridge) interactRedrawStateful(postID, actionID, selected string) *model.Post {
	if postID == "" {
		return nil
	}
	if selected != "" {
		p.mu.Lock()
		if p.formState == nil {
			p.formState = make(map[string]map[string]string)
		}
		f := p.formState[postID]
		if f == nil {
			f = make(map[string]string)
			p.formState[postID] = f
		}
		f[actionID] = selected
		p.mu.Unlock()
	}
	post, err := p.client.Post.GetPost(postID)
	if err != nil || post == nil {
		return nil
	}
	updated := post.Clone()
	srcProps := post.GetProps()
	props := make(map[string]any, len(srcProps))
	for k, v := range srcProps {
		props[k] = v
	}

	// Snapshot the full selection state (all controls) for this post.
	p.mu.Lock()
	sel := make(map[string]string, len(p.formState[postID]))
	for k, v := range p.formState[postID] {
		sel[k] = v
	}
	p.mu.Unlock()

	// Rebuild attachments: keep EVERY action so other controls stay live, but show
	// each control's accumulated selection as a compact progress line. The selected
	// values also ride props.selected so Hermes can read them without parsing the
	// wire format.
	attachments, _ := props["attachments"].([]any)
	built := make([]any, 0, len(attachments))
	allSel := []string{}
	for _, a := range attachments {
		am, ok := a.(map[string]any)
		if !ok {
			built = append(built, a)
			continue
		}
		cp := make(map[string]any, len(am))
		for k, v := range am {
			cp[k] = v
		}
		base := ""
		if s, ok := cp["text"].(string); ok {
			// If this is not the first redraw, strip any previous progress block
			// so we don't compound "Выбрано:" lines on every click.
			if i := strings.Index(s, "\n\n**Выбор:**"); i >= 0 {
				base = strings.TrimSpace(s[:i])
			} else {
				base = strings.TrimSpace(s)
			}
		}
		lines := []string{}
		if acts, ok := cp["actions"].([]any); ok {
			for _, ac := range acts {
				am2, ok := ac.(map[string]any)
				if !ok {
					continue
				}
				aid, _ := am2["id"].(string)
				name, _ := am2["name"].(string)
				if name == "" {
					name = aid
				}
				// Mark the control's current value if one is in state.
				if v, ok := sel[aid]; ok && v != "" {
					lines = append(lines, "**"+name+":** "+v)
					allSel = append(allSel, name+"="+v)
				}
			}
		}
		txt := base
		if len(lines) > 0 {
			txt = base + "\n\n**Выбор:**\n" + strings.Join(lines, "\n")
		}
		cp["text"] = txt
		// Copy actions back unchanged — nothing removed.
		built = append(built, cp)
	}
	if len(built) > 0 {
		props["attachments"] = built
	}
	props["selected"] = strings.Join(allSel, "; ")
	updated.SetProps(props)
	return updated
}

// interactRedrawPost returns an updated post with the interactive actions cleared
// (buttons/menus disappear, no second choice) while keeping the original question
// and the chosen answer. The answer is appended to the attachment text (which
// Mattermost reliably renders/saves on update) and recorded in props.selected so
// the agent can read it without parsing transport details.
func (p *Bridge) interactRedrawPost(postID, selected string) *model.Post {
	post, err := p.client.Post.GetPost(postID)
	if err != nil || post == nil {
		return nil
	}
	updated := post.Clone()
	srcProps := post.GetProps()
	props := make(map[string]any, len(srcProps))
	for k, v := range srcProps {
		props[k] = v
	}
	attachments, _ := props["attachments"].([]any)
	stripped := make([]any, 0, len(attachments))
	for _, a := range attachments {
		am, ok := a.(map[string]any)
		if !ok {
			stripped = append(stripped, a)
			continue
		}
		cp := make(map[string]any, len(am))
		for k, v := range am {
			cp[k] = v
		}
		delete(cp, "actions") // remove buttons/menu -> no second choice
		if selected != "" {
			base := ""
			if s, ok := cp["text"].(string); ok {
				base = s
			}
			if strings.TrimSpace(base) == "" {
				base = "Выбор"
			}
			cp["text"] = base + "\n\n**Выбрано:** " + selected
		}
		stripped = append(stripped, cp)
	}
	if len(stripped) > 0 {
		props["attachments"] = stripped
	} else {
		delete(props, "attachments")
	}
	if selected != "" {
		props["selected"] = selected
	}
	updated.SetProps(props)
	return updated
}

func main() {
	plugin.ClientMain(&Bridge{})
}

func registryCommand(spec CommandSpec) *model.Command {
	// Method "P" = POST (the default for plugin slash commands).
	return &model.Command{
		Trigger:          spec.Trigger,
		AutoComplete:     spec.AutoComplete,
		AutoCompleteDesc: spec.Description,
		AutoCompleteHint: spec.Hint,
		Method:           model.CommandMethodPost,
	}
}

func writeErr(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": msg})
}

func jsonOk(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(v)
}
