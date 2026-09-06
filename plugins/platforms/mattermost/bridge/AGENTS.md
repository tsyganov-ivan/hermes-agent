# mattermost-bridge — Mattermost server plugin (native transport)

This directory is the **native Go** companion to the Hermes Mattermost adapter in this
same package (`../adapter.py`). It runs *inside* the Mattermost server (installed once,
via the admin bot token — no SSH) and relays UI inputs to the Hermes bot that stays
behind NAT:

- **Slash commands** → `ExecuteCommand` hook → `PublishWebSocketEvent("hermes_bridge_command", …, broadcast={ChannelId})` targeting the channel the command ran in.
- **Buttons/menus** → MM server POSTs locally to `/plugins/<plugin_id>/interact` →
  `ServeHTTP` → `PublishWebSocketEvent("hermes_bridge_interact", …)`.

**Interactive model is deliberately simple (finalized 2026-09-06):** a post is **EITHER
several buttons OR one select menu — never both** (mixing is rejected by
`adapter.send_interactive`). There is NO multi-control form accumulation, NO Submit
button, and NO `form_state`/`submission`/`final` in the WS payload. Every click/selection
is a FINAL choice: the plugin relays `hermes_bridge_interact` (`action_id` +
`selected_option`/`label`) then disables-after-pick on the post (clears the `actions[]`
so no second choice). The plugin holds no state and never rewrites a live post's actions.
Why: MM strips `actions[].integration` on `GET /posts` and `PUT /posts/{id}/patch` fully
replaces props — a round-trip redraw wipes every button (the earlier stateful form
machinery was removed for this reason). Multi-field forms belong in native dialogs
(`send_dialog`/`OpenInteractiveDialog`), which the plugin relays without touching posts.
- **Interactive dialogs** (2 hops, both dumb):
  1. `POST /plugins/<plugin_id>/interact` with a `context.dialog` schema (posted by Hermes).
     `handleInteract` sees the key and calls `p.API.OpenInteractiveDialog({TriggerId,
     URL: "/plugins/hermes-bridge/dialog", Dialog: <decoded schema>})` → the modal opens
     in the clicking user's client. No choice WS event is emitted (opening a form ≠ a choice).
  2. On submit/cancel the MM server POSTs a `SubmitDialogRequest` to `/plugins/<plugin_id>/dialog`
     → `handleDialogSubmit` → `PublishWebSocketEvent("hermes_bridge_dialog", …)` carrying
     `callback_id`/`state`/`submission`/`cancelled`/user/channel. The `state` field echoes
     Hermes' `question_id` so the submit is correlated back to the dialog.
- **Command registry** → Hermes `POST /plugins/<plugin_id>/config` with `Authorization: Bearer <shared_secret>`.

The plugin holds **no command logic**. It is a dumb receiver/relay: Hermes owns the
single command registry (mirrors Slack's `_command_handler_table`) and pushes it here
by REST; the plugin `RegisterCommand`s each entry and forwards invocations verbatim.
Routing "whose channel → which bot replies" happens on the Hermes host. WS broadcasts
are addressed **by channel** (`broadcast.ChannelId`), never a hard-coded bot: any bot
serving that channel's conversation picks the event up and Hermes-side routing decides
who answers (per `design/mattermost-plugin-transport.md`).

See `design/mattermost-plugin-transport.md` (repo root) for the full transport/protocol
design and decisions (registry channel = REST; shared registry; no event queue).

## Command registry auto-sync (Hermes side)

`../bridge_registry.py` gathers gateway-usable slash commands from
`hermes_cli.commands.COMMAND_REGISTRY`, namespaces each with a configurable prefix
(default `hermes:`, so they never collide with built-in Mattermost commands like
`/help` or `/status`), and pushes them to the plugin's `/config` REST endpoint **once
at gateway connect** (best-effort, fail-open — a failed sync never blocks startup).

- Config: `mattermost.bridge_command_prefix` (default `hermes`),
  `mattermost.bridge_shared_secret` (required to enable sync), and
  `mattermost.bridge_plugin_path` (default `plugins/hermes-bridge/config`). Brinded
  through `_YAML_BRIDGE`/env (`MATTERMOST_BRIDGE_COMMAND_PREFIX`, `_BRIDGE_SECRET`,
  `_BRIDGE_PLUGIN_PATH`).
- On inbound, `_handle_bridge_command` strips the namespace prefix so the runner
  dispatches the canonical name (`hermes:new` → `/new`).
- `cli_only` commands (without a `gateway_config_gate`) are skipped; aliases are not
  emitted (they reuse the same handler via the dispatch table).
- Registry sync is idempotent: `POST /config` with `replace:true` overwrites the
  plugin's command set (single source of truth = the Hermes registry).

## Layout (Mattermost plugin convention)

```
bridge/
├── plugin.json            # manifest; server.executables => server/dist/plugin-linux-{amd64,arm64}
├── Makefile               # make dist → builds + bundles dist/hermes-bridge-<version>.tar.gz
├── server/                # Go module (own go.mod); imports github.com/mattermost/mattermost/server/public
│   ├── plugin.go          # Bridge: ExecuteCommand + ServeHTTP + config endpoint + registry
│   └── dist/              # build artifacts (gitignored)
└── dist/                  # packaged tar.gz (gitignored)
```

## Building

```bash
make dist          # compiles linux/{amd64,arm64}, packs dist/hermes-bridge-0.1.0.tar.gz
cd server && go vet ./...
```

The bundle (root folder `hermes-bridge/` + `plugin.json`) is installable via
`POST /api/v4/plugins` with the admin bot token — see the `hermes-administration` skill,
"Mattermost plugin install".

## Verified SDK facts (2026-09-05, server/public@v0.1.21)

- **Server SDK import path is `github.com/mattermost/mattermost/server/public`** (not
  `/server/v8`). The `/v8` module's dev snapshots no longer carry the model/plugin
  packages (verified: `go get .../server/v8@latest` resolves to a snapshot without them).
- **Toolchain**: released `server/public` tags build with Go 1.24 (installed locally).
  Do NOT `go get ...@latest` — dev snapshots require Go ≥ 1.26. Pin released versions.
- **`pluginapi.NewClient(api plugin.API, driver plugin.Driver) *Client`** — use
  `p.API` / `p.Driver` from the embedded `plugin.MattermostPlugin`. Construct in
  `OnConfigurationChange`. No `model.NewDriver` / `plugin.ClientMain(somethingElse)`:
  entry point is `plugin.ClientMain(&Bridge{})` where `Bridge` embeds `MattermostPlugin`.
- **`ExecuteCommand(c *Context, args *model.CommandArgs) (*model.CommandResponse, *model.AppError)`**.
  `CommandArgs` has NO `ChannelType()` / `UserName` helpers — resolve `channel_type` via
  `p.API.GetChannel(args.ChannelId)` (its `Type` field) and `user_name` via
  `p.API.GetUser(args.UserId)`. Thread-root id is `args.RootId`.
- **`PublishWebSocketEvent(event string, data map[string]any, broadcast *model.WebsocketBroadcast)`**.
  Target all bots in a channel with `&model.WebsocketBroadcast{ChannelId: <channel_id>}`
  (this plugin addresses by channel, not by a single `UserId`, so routing stays
  Hermes-side).
- **Registry API**: `RegisterCommand(*model.Command) error`, `UnregisterCommand(teamID,
  trigger string) error`. `Command.Method` must be `model.CommandMethodPost` ("P") for
  plugin slash commands. `UnregisterCommand` is **per-team** — pass the teamID the
  invoked command lives under (currently empty; reassess if multi-team routing arrives).
- **Ephemeral placeholder** is returned from `ExecuteCommand` so the client does not
  hang; the real answer is delivered by the bot itself over normal posting.

## NOT in this plugin

- Reactions (read/set) and message send/receive already live in the Python adapter
  (`../adapter.py` + `../react_message_tool.py` on the fork branch). Don't duplicate them
  in Go.
- Webapp component. UI interactions are relayed, not reimplemented.

## Tests

Server-side Go tests live in `server/` (per-package). Adapter-side parsing/dispatch for
the WS event branches lives with the Python adapter under `tests/gateway/`.