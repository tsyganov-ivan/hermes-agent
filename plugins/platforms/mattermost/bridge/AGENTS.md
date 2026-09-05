# mattermost-bridge — Mattermost server plugin (native transport)

This directory is the **native Go** companion to the Hermes Mattermost adapter in this
same package (`../adapter.py`). It runs *inside* the Mattermost server (installed once,
via the admin bot token — no SSH) and relays UI inputs to the Hermes bot that stays
behind NAT:

- **Slash commands** → `ExecuteCommand` hook → `PublishWebSocketEvent("hermes_bridge_command", …, broadcast={UserId: bot})`.
- **Buttons/menus/dialogs** → MM server POSTs locally to `/plugins/<plugin_id>/…` →
  `ServeHTTP` → `PublishWebSocketEvent("hermes_bridge_interact", …)`.
- **Command registry** → Hermes `POST /plugins/<plugin_id>/config` with `Authorization: Bearer <shared_secret>`.

The plugin holds **no command logic**. It is a dumb receiver/relay: Hermes owns the
single command registry (mirrors Slack's `_command_handler_table`) and pushes it here
by REST; the plugin `RegisterCommand`s each entry and forwards invocations verbatim.
Routing "whose channel → which bot replies" happens on the Hermes host (broadcast by
`UserId` makes events non-racy).

See `design/mattermost-plugin-transport.md` (repo root) for the full transport/protocol
design and decisions (registry channel = REST; shared registry; no event queue).

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
  Target one bot with `&model.WebsocketBroadcast{UserId: <bot_user_id>}`.
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