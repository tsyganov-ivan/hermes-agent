package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin"
	"github.com/mattermost/mattermost/server/public/pluginapi"
)

// bridgeTestAPI is a minimal plugin.API stub capturing the calls we assert on.
type bridgeTestAPI struct {
	plugin.API
	fakeUpstream plugin.API

	openDialogArgs []model.OpenDialogRequest
	wsEvents       []struct{ event string; payload map[string]any }
	lastGetUser    string
	posts          map[string]*model.Post // for GetPost/UpdatePost in redraw path
}

func (a *bridgeTestAPI) OpenInteractiveDialog(dialog model.OpenDialogRequest) *model.AppError {
	a.openDialogArgs = append(a.openDialogArgs, dialog)
	return nil
}

func (a *bridgeTestAPI) PublishWebSocketEvent(event string, payload map[string]any, broadcast *model.WebsocketBroadcast) {
	a.wsEvents = append(a.wsEvents, struct {
		event   string
		payload map[string]any
	}{event, payload})
}

func (a *bridgeTestAPI) GetUser(userID string) (*model.User, *model.AppError) {
	a.lastGetUser = userID
	if userID == "u_submit" {
		return &model.User{Username: "sam"}, nil
	}
	return &model.User{Username: "user"}, nil
}

func (a *bridgeTestAPI) GetPost(postID string) (*model.Post, *model.AppError) {
	if a.posts == nil {
		return nil, &model.AppError{Id: "not_found", StatusCode: http.StatusNotFound}
	}
	p, ok := a.posts[postID]
	if !ok {
		return nil, &model.AppError{Id: "not_found", StatusCode: http.StatusNotFound}
	}
	return p.Clone(), nil
}

func (a *bridgeTestAPI) UpdatePost(post *model.Post) (*model.Post, *model.AppError) {
	if a.posts == nil {
		a.posts = map[string]*model.Post{}
	}
	a.posts[post.Id] = post.Clone()
	return a.posts[post.Id], nil
}

func (a *bridgeTestAPI) LoadPluginConfiguration(dest any) error {
	return nil
}

func (a *bridgeTestAPI) LogInfo(_ string, _ ...any) {}

func newBridge() *Bridge {
	b := &Bridge{}
	b.API = &bridgeTestAPI{posts: map[string]*model.Post{}}
	b.cfg = &configuration{SharedSecret: "sec"}
	b.client = pluginapi.NewClient(b.API, nil)
	return b
}

func doServeHTTP(b *Bridge, path string, body any) *httptest.ResponseRecorder {
	var rdr *bytes.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(http.MethodPost, path, rdr)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	// Context carries the request's user id so OpenInteractiveDialog targets the caller.
	b.ServeHTTP(&plugin.Context{}, rec, req)
	return rec
}

// A button click whose integration.context carries a "dialog" schema must open
// the dialog via OpenInteractiveDialog (trigger_id), routed to the local /dialog
// URL — and NOT relay a separate interact event (opening a form is not a choice).
func TestDialogOpenFromInteractContext(t *testing.T) {
	b := newBridge()
	api := b.API.(*bridgeTestAPI)

	dialogSchema := map[string]any{
		"callback_id":       "report",
		"title":             "Report",
		"introduction_text": "Fill the report",
		"submit_label":      "Send",
		"elements": []any{
			map[string]any{"name": "summary", "display_name": "Summary", "type": "textarea"},
			map[string]any{"name": "priority", "display_name": "Priority", "type": "select",
				"options": []any{map[string]any{"text": "High", "value": "high"},
					map[string]any{"text": "Low", "value": "low"}}},
		},
	}
	body := map[string]any{
		"user_id": "u1", "user_name": "op", "channel_id": "chan_9", "channel_name": "ops",
		"team_id": "t1", "post_id": "post_9", "trigger_id": "trig_xyz",
		"type": "button", "data_source": "",
		"context": map[string]any{"action_id": "report", "dialog": dialogSchema},
	}
	rec := doServeHTTP(b, "/interact", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if len(api.openDialogArgs) != 1 {
		t.Fatalf("expected 1 OpenInteractiveDialog call, got %d", len(api.openDialogArgs))
	}
	odr := api.openDialogArgs[0]
	if odr.TriggerId != "trig_xyz" {
		t.Errorf("expected trigger_id trig_xyz, got %q", odr.TriggerId)
	}
	if odr.URL != dialogURL {
		t.Errorf("expected URL %q, got %q", dialogURL, odr.URL)
	}
	if odr.Dialog.CallbackId != "report" {
		t.Errorf("expected callback_id report, got %q", odr.Dialog.CallbackId)
	}
	if len(odr.Dialog.Elements) != 2 {
		t.Fatalf("expected 2 elements, got %d", len(odr.Dialog.Elements))
	}
	if odr.Dialog.Elements[0].Name != "summary" || odr.Dialog.Elements[0].Type != "textarea" {
		t.Errorf("element[0] mismatch: %+v", odr.Dialog.Elements[0])
	}
	if len(odr.Dialog.Elements[1].Options) != 2 {
		t.Errorf("expected 2 select options, got %d", len(odr.Dialog.Elements[1].Options))
	}
	if odr.Dialog.Elements[1].Options[0].Value != "high" {
		t.Errorf("expected select option[0].value high, got %q", odr.Dialog.Elements[1].Options[0].Value)
	}
	// A dialog-open click must NOT emit an interact WS event.
	if len(api.wsEvents) != 0 {
		t.Errorf("expected no WS events for dialog open, got %d", len(api.wsEvents))
	}
}

// Same as above but via the namespaced WS route we don't test here — ensure a
// bad schema or missing trigger_id returns an error without opening.
func TestDialogOpenMissingTriggerReturns400(t *testing.T) {
	b := newBridge()
	api := b.API.(*bridgeTestAPI)
	body := map[string]any{
		"user_id": "u1", "channel_id": "chan_9", "post_id": "post_9",
		"type": "button", "trigger_id": "",
		"context": map[string]any{
			"action_id": "x",
			"dialog":    map[string]any{"callback_id": "c", "title": "T"},
		},
	}
	rec := doServeHTTP(b, "/interact", body)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for missing trigger_id, got %d", rec.Code)
	}
	if len(api.openDialogArgs) != 0 {
		t.Fatal("dialog must not be opened without trigger_id")
	}
}

// A dialog submission (SubmitDialogRequest) must be relayed as a structured
// hermes_bridge_dialog WS event with the opaque submission map + user/channel.
func TestDialogSubmitRelaysWS(t *testing.T) {
	b := newBridge()
	api := b.API.(*bridgeTestAPI)
	body := model.SubmitDialogRequest{
		Type:       "dialog_submission",
		CallbackId: "report",
		State:      "qid_abc",
		UserId:     "u_submit",
		ChannelId:  "chan_9",
		TeamId:     "t1",
		Submission: map[string]any{"summary": "All good", "priority": "high"},
	}
	rec := doServeHTTP(b, "/dialog", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if len(api.wsEvents) != 1 {
		t.Fatalf("expected 1 WS event, got %d", len(api.wsEvents))
	}
	evt := api.wsEvents[0]
	if evt.event != wsExitDialog {
		t.Errorf("expected event %q, got %q", wsExitDialog, evt.event)
	}
	payload := evt.payload
	if payload["callback_id"] != "report" {
		t.Errorf("expected callback_id report, got %v", payload["callback_id"])
	}
	if payload["state"] != "qid_abc" {
		t.Errorf("expected state qid_abc, got %v", payload["state"])
	}
	if payload["cancelled"] != false {
		t.Errorf("expected cancelled false, got %v", payload["cancelled"])
	}
	if payload["user_name"] != "sam" {
		t.Errorf("expected user_name sam (resolved via GetUser), got %v", payload["user_name"])
	}
	sub, ok := payload["submission"].(map[string]any)
	if !ok || sub["summary"] != "All good" {
		t.Errorf("submission not relayed verbatim: %v", payload["submission"])
	}
}

// A cancelled dialog is relayed the same way with cancelled=true.
func TestDialogCancelRelaysWS(t *testing.T) {
	b := newBridge()
	api := b.API.(*bridgeTestAPI)
	body := model.SubmitDialogRequest{
		Type: "dialog_cancelled", CallbackId: "report", State: "qid_abc",
		UserId: "u_submit", ChannelId: "chan_9", Cancelled: true,
	}
	rec := doServeHTTP(b, "/dialog", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	evt := api.wsEvents[0]
	if evt.payload["cancelled"] != true {
		t.Errorf("expected cancelled true, got %v", evt.payload["cancelled"])
	}
}

// A button/menu click in the simple interactive model is a FINAL choice: the
// plugin relays it as a structured WS event (action_id + selected), then
// disables-after-pick by clearing every action on the post (no second choice).
func TestInteractClickIsFinalAndDisables(t *testing.T) {
	b := newBridge()
	api := b.API.(*bridgeTestAPI)
	api.posts["pick_1"] = &model.Post{Id: "pick_1", ChannelId: "chan_9",
		Props: map[string]any{"attachments": []any{
			map[string]any{
				"text": "Choose",
				"actions": []any{
					map[string]any{"id": "a", "type": "button", "name": "A"},
					map[string]any{"id": "b", "type": "button", "name": "B"},
				},
			},
		}}}

	rec := doServeHTTP(b, "/interact", map[string]any{
		"user_id": "u1", "user_name": "op", "channel_id": "chan_9", "post_id": "pick_1",
		"type": "button", "context": map[string]any{"action_id": "a", "label": "A"},
		"trigger_id": "trig",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}

	// First: the choice was relayed as a hermes_bridge_interact WS event with the
	// action_id + selected label (text choice, not an invented command).
	if len(api.wsEvents) != 1 {
		t.Fatalf("expected 1 WS event, got %d", len(api.wsEvents))
	}
	evt := api.wsEvents[0]
	if evt.event != wsExitInteract {
		t.Errorf("expected event %q, got %q", wsExitInteract, evt.event)
	}
	if evt.payload["action_id"] != "a" {
		t.Errorf("expected action_id a, got %v", evt.payload["action_id"])
	}
	if evt.payload["selected_option"] != "A" {
		t.Errorf("expected selected_option A, got %v", evt.payload["selected_option"])
	}
	if evt.payload["post_id"] != "pick_1" {
		t.Errorf("expected post_id pick_1, got %v", evt.payload["post_id"])
	}
	if _, hasFinal := evt.payload["final"]; hasFinal {
		t.Error("payload must not carry a final flag (every click is final)")
	}

	// Second: disable-after-pick — all actions cleared, choice recorded.
	stored, _ := api.posts["pick_1"]
	props := stored.GetProps()
	atts := props["attachments"].([]any)
	am := atts[0].(map[string]any)
	if acts, ok := am["actions"].([]any); ok && len(acts) != 0 {
		t.Fatalf("expected actions cleared after pick, got %v", am["actions"])
	}
	txt := am["text"].(string)
	if !containsStr(txt, "A") {
		t.Errorf("expected choice A recorded in text, got %q", txt)
	}
}

func containsStr(haystack, needle string) bool {
	return strings.Contains(haystack, needle)
}

// A menu selection is the same final path with the select's chosen value.
func TestInteractMenuSelectionIsFinal(t *testing.T) {
	b := newBridge()
	api := b.API.(*bridgeTestAPI)
	api.posts["menu_1"] = &model.Post{Id: "menu_1", ChannelId: "chan_9",
		Props: map[string]any{"attachments": []any{
			map[string]any{
				"text": "Pick one",
				"actions": []any{
					map[string]any{"id": "opt", "type": "select", "name": "Opt",
						"options": []any{
							map[string]any{"text": "S", "value": "s"},
							map[string]any{"text": "L", "value": "l"}}},
				},
			},
		}}}

	rec := doServeHTTP(b, "/interact", map[string]any{
		"user_id": "u1", "user_name": "op", "channel_id": "chan_9", "post_id": "menu_1",
		"type": "select", "context": map[string]any{"action_id": "opt", "selected_option": "l"},
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if len(api.wsEvents) != 1 {
		t.Fatalf("expected 1 WS event, got %d", len(api.wsEvents))
	}
	if api.wsEvents[0].payload["selected_option"] != "l" {
		t.Errorf("expected selected_option l, got %v", api.wsEvents[0].payload["selected_option"])
	}
	// Actions cleared after the menu pick too.
	stored, _ := api.posts["menu_1"]
	props := stored.GetProps()
	atts := props["attachments"].([]any)
	am := atts[0].(map[string]any)
	if acts, ok := am["actions"].([]any); ok && len(acts) != 0 {
		t.Fatalf("expected menu actions cleared after pick, got %v", am["actions"])
	}
}