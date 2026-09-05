package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"strings"

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
}

// configuration mirrors the server-side plugin settings.
type configuration struct {
	// SharedSecret must match the one kept by the Hermes host so the
	// command-registry REST endpoint (/plugins/<id>/config) is authenticated.
	SharedSecret string `json:"shared_secret"`
	// BotUserID names the MM user owning the Hermes bot that should receive the
	// custom WS events. The plugin addresses broadcasts to it (broadcast.UserId
	// = BotUserID) so the event reaches exactly that Hermes gateway — not the
	// invoking human user.
	BotUserID string `json:"bot_user_id"`
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
	target := p.broadcastToBot()
	p.API.PublishWebSocketEvent(wsExitCommand, payload, target)

	return &model.CommandResponse{
		ResponseType: model.CommandResponseTypeEphemeral,
		Text:         "Запрос отправлен боту.",
	}, nil
}

// broadcastToBot returns a WebsocketBroadcast addressing the Hermes bot (from
// config.BotUserID). With no configured bot user, the event is broadcast to all
// listeners (null only as a fallback for misconfiguration).
func (p *Bridge) broadcastToBot() *model.WebsocketBroadcast {
	if p.cfg != nil && p.cfg.BotUserID != "" {
		return &model.WebsocketBroadcast{UserId: p.cfg.BotUserID}
	}
	return &model.WebsocketBroadcast{}
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
	default:
		if r.Method != http.MethodPost {
			writeErr(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		p.handleInteract(w, r)
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
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		writeErr(w, http.StatusBadRequest, "read body: "+err.Error())
		return
	}
	var raw map[string]any
	_ = json.Unmarshal(body, &raw)
	p.API.PublishWebSocketEvent(wsExitInteract, map[string]any{"raw": raw}, p.broadcastToBot())
	jsonOk(w, map[string]any{"ok": true})
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