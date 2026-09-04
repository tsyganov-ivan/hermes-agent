"""Model-callable tool to attach/retract an emoji reaction on a message.

``send_message`` is deliberately NOT registered as an agent-callable model tool
(see tools/send_message_tool.py), which also hides its ``react``/``unreact``
actions. This tool makes reactions directly available to the agent so a Mattermost
(and any other supported) bot can ack/like arbitrary messages with an emoji.

The implementation is the same battle-tested ``_handle_react`` used by send_message
action='react'; this file only exposes it as a first-class model tool so gateways
don't need the full cross-platform send_message surface to react.
"""

from __future__ import annotations

from tools.registry import registry
from tools.send_message_tool import _handle_react


def react_message(args: dict, **kw) -> str:
    """Attach or retract an emoji reaction on a message via a connected platform."""
    action = str(args.get("action") or "react")
    if action not in ("react", "unreact"):
        return '{success:false,error:action must be react or unreact}'
    return _handle_react(args, remove=action == "unreact")


registry.register(
    name="react_message",
    toolset="message_reactions",
    schema={
        "name": "react_message",
        "description": (
            "Attach (action='react') or retract (action='unreact') an emoji reaction on a message "
            "on a connected messaging platform (e.g. Mattermost). Lets the bot ack/like/signal on "
            "any message the platform exposes. Requires the live gateway adapter (not available in "
            "cron/standalone contexts)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["react", "unreact"],
                    "description": "react attaches the emoji, unreact retracts the bot's existing reaction.",
                },
                "target": {
                    "type": "string",
                    "description": "Platform target: 'platform', 'platform:chat_id', or "
                                    "'platform:chat_id:thread_id'. e.g. 'mattermost', "
                                    "'mattermost:5ap78uro47rbpqce3fh4'.",
                },
                "message_id": {
                    "type": "string",
                    "description": "id of the message to react to. Omit to target the most recent "
                                    "message in that chat.",
                },
                "emoji": {
                    "type": "string",
                    "description": "The emoji to react with, e.g. '👍', '❤️', '✅'. Required for "
                                    "action='react'.",
                },
            },
            "required": ["target"],
        },
    },
    handler=lambda args, **kw: react_message(args, **kw),
    check_fn=lambda: True,
)