"""Model-callable tools for Mattermost interactive messages (buttons/menus/ephemeral).

All MM transport detail is hidden from the bot — it never sees integration URLs,
callback payloads, or the native plugin. Each tool targets a conversation via
``mattermost:chat_id`` and talks to the live gateway adapter, which handles the
attachment+actions wire format internally.

Requires the live gateway adapter (not available from cron/standalone contexts),
same as ``react_message``.
"""

from __future__ import annotations

import json
from typing import Optional

from tools.registry import registry, tool_error
from tools.send_message_tool import (
    _dispatch_on_gateway_loop, _resolve_tool_target, _platform_enum,
)
from tools.send_message_senders import _live_adapter


def _target(chat_id: str, thread_id: Optional[str] = None, reply_to: Optional[str] = None) -> tuple:
    platform_name, cid, tid, error = _resolve_tool_target(
        chat_id, pass_unresolved_references=True)
    if error:
        return None, None, error
    platform, err = _platform_enum(platform_name)
    if err:
        return None, None, err
    if platform_name != "mattermost":
        return None, None, "Interactive messages are only supported on mattermost, got " + platform_name
    return platform, cid, tid


def send_interactive_message(args: dict, **kw) -> str:
    """Send a post with interactive buttons and/or a select menu to a Mattermost conversation."""
    target = (args.get("target") or "").strip()
    text = (args.get("text") or "").strip()
    buttons = args.get("buttons") or []
    menu = args.get("menu")
    if not target:
        return tool_error("'target' is required (e.g. 'mattermost:chat_id').")
    if not text:
        return tool_error("'text' is required.")
    platform, chat_id, thread_id = _target(target)
    if chat_id is None:
        return tool_error(thread_id)
    # If the bot is replying inside a thread, inherit the current thread when the
    # target names only the channel — otherwise buttons land in the channel root.
    if not thread_id:
        from gateway.session_context import get_session_env
        th = (get_session_env("HERMES_SESSION_THREAD_ID", "") or "").strip()
        if th:
            thread_id = th
    # In a DM/plain channel there is no thread root — inherit the message being
    # answered so buttons post as a reply, not as a brand-new root post.
    reply_to = thread_id
    if not reply_to:
        from gateway.session_context import get_session_env as _gse
        mid = (_gse("HERMES_SESSION_MESSAGE_ID", "") or "").strip()
        if mid:
            reply_to = mid
    _, adapter = _live_adapter(platform)
    if adapter is None:
        return tool_error("Interactive messages require a live mattermost adapter "
                          "(not available from cron/standalone contexts).")
    send_fn = getattr(adapter, "send_interactive", None)
    if not callable(send_fn):
        return tool_error("Mattermost adapter does not support send_interactive.")
    try:
        from model_tools import _run_async

        async def _coro():
            return await _dispatch_on_gateway_loop(
                _live_adapter(platform)[0],
                lambda: send_fn(chat_id=chat_id, text=text, buttons=buttons, menu=menu,
                                reply_to=reply_to),
                "mattermost_interact: failed to schedule send_interactive on gateway loop")

        result = _run_async(_coro())
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"send_interactive failed: {e}"})
    if isinstance(result, dict):
        result.pop("raw_response", None)  # don't leak raw transport internals to the model
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


def update_message(args: dict, **kw) -> str:
    """Replace the text of an interactive message on a Mattermost channel (drop the buttons)."""
    target = (args.get("target") or "").strip()
    message_id = (args.get("message_id") or "").strip()
    text = (args.get("text") or "").strip()
    if not target:
        return tool_error("'target' is required (e.g. 'mattermost:chat_id').")
    if not message_id:
        return tool_error("'message_id' is required — update a SPECIFIC message (post id).")
    platform, chat_id, _thread_id = _target(target)
    if chat_id is None:
        return tool_error(_thread_id)
    _, adapter = _live_adapter(platform)
    if adapter is None:
        return tool_error("Updating a message requires a live mattermost adapter.")
    update_fn = getattr(adapter, "edit_message", None)
    if not callable(update_fn):
        return tool_error("Mattermost adapter does not support edit_message.")
    try:
        from model_tools import _run_async

        async def _coro():
            return await _dispatch_on_gateway_loop(
                _live_adapter(platform)[0],
                lambda: update_fn(chat_id=chat_id, message_id=message_id, content=text),
                "mattermost_interact: failed to schedule update_message on gateway loop")

        result = _run_async(_coro())
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"update_message failed: {e}"})
    if isinstance(result, dict):
        result.pop("raw_response", None)
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


def ephemeral_reply(args: dict, **kw) -> str:
    """Send a message visible only to one user in a Mattermost channel (ephemeral)."""
    target = (args.get("target") or "").strip()
    user_id = (args.get("user_id") or "").strip()
    text = (args.get("text") or "").strip()
    if not target:
        return tool_error("'target' is required (e.g. 'mattermost:chat_id').")
    if not user_id:
        return tool_error("'user_id' is required — the Mattermost user who should see this.")
    if not text:
        return tool_error("'text' is required.")
    platform, chat_id, _thread_id = _target(target)
    if chat_id is None:
        return tool_error(_thread_id)
    _, adapter = _live_adapter(platform)
    if adapter is None:
        return tool_error("Ephemeral replies require a live mattermost adapter.")
    send_fn = getattr(adapter, "send_ephemeral", None)
    if not callable(send_fn):
        return tool_error("Mattermost adapter does not support send_ephemeral.")
    try:
        from model_tools import _run_async

        async def _coro():
            return await _dispatch_on_gateway_loop(
                _live_adapter(platform)[0],
                lambda: send_fn(chat_id=chat_id, user_id=user_id, text=text),
                "mattermost_interact: failed to schedule ephemeral_reply on gateway loop")

        result = _run_async(_coro())
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"ephemeral_reply failed: {e}"})
    if isinstance(result, dict):
        result.pop("raw_response", None)
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


registry.register(
    name="send_interactive_message",
    toolset="message_interactive",
    schema={
        "name": "send_interactive_message",
        "description": (
            "Send a post with interactive buttons and/or a select menu to a Mattermost "
            "conversation. The bot provides message text, buttons=[{id, label, style}] and/or "
            "menu={id, name, placeholder, options:[{label, value}]}. target is "
            "'mattermost:chat_id' (optionally ':...thread_id' to post into a thread). "
            "Clicks/menu selections come back to the agent as user "
            "messages. Use update_message to replace the post text afterwards, and "
            "ephemeral_reply to answer just one user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "text": {"type": "string", "description": "Post text shown above the buttons/menu."},
                "buttons": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Unique action id (returned on click)."},
                            "label": {"type": "string", "description": "Button label."},
                            "style": {"type": "string", "description": "default|primary|success|warning|danger"},
                        },
                    },
                    "description": "Optional list of buttons.",
                },
                "menu": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "placeholder": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                            },
                        },
                    },
                    "description": "Optional single select menu: {id, placeholder, options:[{label, value}]}.",
                },
            },
            "required": ["target", "text"],
        },
    },
    handler=lambda args, **kw: send_interactive_message(args, **kw),
)

registry.register(
    name="update_message",
    toolset="message_interactive",
    schema={
        "name": "update_message",
        "description": (
            "Replace the text of an existing interactive Mattermost post (e.g. to show the chosen "
            "option or drop the now-stale buttons). Requires message_id (the exact post id) and "
            "target 'mattermost:chat_id'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "message_id": {"type": "string", "description": "REQUIRED. The post id of the interactive message to update."},
                "text": {"type": "string", "description": "New message text (buttons are removed)."},
            },
            "required": ["target", "message_id", "text"],
        },
    },
    handler=lambda args, **kw: update_message(args, **kw),
)

registry.register(
    name="ephemeral_reply",
    toolset="message_interactive",
    schema={
        "name": "ephemeral_reply",
        "description": (
            "Send a Mattermost message visible only to one user in a channel (ephemeral) — ideal for "
            "private answers or errors. Requires target 'mattermost:chat_id', user_id (the Mattermost "
            "user id), and text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "user_id": {"type": "string", "description": "REQUIRED. Mattermost user id who should see this."},
                "text": {"type": "string", "description": "Ephemeral message text."},
            },
            "required": ["target", "user_id", "text"],
        },
    },
    handler=lambda args, **kw: ephemeral_reply(args, **kw),
)