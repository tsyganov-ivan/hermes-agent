// Package main implements the hermes-bridge Mattermost server plugin.
// It is the native-side transport companion to the Hermes Mattermost adapter:
// it intercepts slash commands (ExecuteCommand) and interactive actions
// (buttons/menus/dialogs via ServeHTTP) and relays them to the Hermes bot
// over the bot's existing outbound WebSocket via PublishWebSocketEvent.
//
// Hermes stays behind NAT; only its outbound WS socket is exposed, mirroring
// the Slack Socket Mode pattern (see design/mattermost-plugin-transport.md).
package main