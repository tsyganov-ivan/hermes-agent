"""Model-callable tools for Mattermost message management (delete / pin / unpin).

All MM transport detail is hidden from the bot — it targets a conversation via
``mattermost:chat_id`` and talks to the live gateway adapter, which drives the
Mattermost v4 REST API internally. Requires the live gateway adapter (not available
from cron/standalone contexts), same as ``message_interactive`` / ``message_reactions``.
"""
from __future__ import annotations

import json
from typing import Callable

from tools.registry import registry, tool_error
from tools.send_message_tool import _dispatch_on_gateway_loop, _platform_enum, _resolve_tool_target
from tools.send_message_senders import _live_adapter


def _resolve(target: str):
    """Resolve ``mattermost:chat_id`` -> ``(platform_enum, chat_id, error_or_None)``."""
    platform_name, cid, _tid, err = _resolve_tool_target(target, pass_unresolved_references=True)
    if err:
        return None, None, err
    platform, perr = _platform_enum(platform_name)
    if perr:
        return None, None, perr
    if platform_name != "mattermost":
        return None, None, f"mattermost_manage supports only mattermost, got {platform_name}"
    return platform, cid, None


def _run_on_loop(platform, fn: Callable[[], object]) -> object:
    """Schedule an async adapter call on the gateway loop; returns its result."""
    from model_tools import _run_async

    async def _coro():
        return await _dispatch_on_gateway_loop(
            _live_adapter(platform)[0],
            fn,
            "mattermost_manage: failed to schedule adapter call on gateway loop")

    return _run_async(_coro())


def _require_manage_method(target: str, method: str):
    """Resolve target + adapter, return (platform, chat_id, callable) or (…, error_str)."""
    platform, chat_id, err = _resolve(target)
    if chat_id is None:
        return None, None, None, err
    _, adapter = _live_adapter(platform)
    fn = getattr(adapter, method, None)
    if not callable(fn):
        return platform, None, None, f"Mattermost adapter does not support {method}"
    return platform, chat_id, fn, None


def delete_message(args: dict, **kw) -> str:
    """Delete a specific Mattermost message the bot owns (the exact post id)."""
    return _manage_void_method(args, "delete_message")


def _manage_void_method(args: dict, method: str) -> str:
    """Shared driver for adapter methods returning a ``{success,...}`` dict."""
    target = (args.get("target") or "").strip()
    message_id = (args.get("message_id") or "").strip()
    if not target:
        return tool_error("'target' is required (e.g. 'mattermost:chat_id').")
    if not message_id:
        return tool_error(f"'{method}' requires message_id — the exact post id.")
    platform, chat_id, fn, err = _require_manage_method(target, method)
    if err:
        return tool_error(err)
    try:
        result = _run_on_loop(platform, lambda: fn(chat_id=chat_id, message_id=message_id))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"{method} failed: {e}"})
    if isinstance(result, dict):
        result.pop("raw_response", None)
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


def pin_message(args: dict, **kw) -> str:
    """Pin a specific Mattermost message (the exact post id)."""
    return _manage_void_method(args, "pin_message")


def unpin_message(args: dict, **kw) -> str:
    """Unpin a specific Mattermost message (the exact post id)."""
    return _manage_void_method(args, "unpin_message")


def search_mattermost_posts(args: dict, **kw) -> str:
    """Search the bot's own Mattermost history (scoped to the team of the target chat)."""
    target = (args.get("target") or "").strip()
    query = (args.get("query") or "").strip()
    if not target:
        return tool_error("'target' is required (e.g. 'mattermost:chat_id').")
    if not query:
        return tool_error("'query' is required.")
    platform, chat_id, err = _resolve(target)
    if chat_id is None:
        return tool_error(err)
    fn = getattr(_live_adapter(platform)[1], "search_posts", None)
    if not callable(fn):
        return tool_error("Mattermost adapter does not support search_posts")
    page = int(args.get("page") or 0)
    per_page = int(args.get("per_page") or 20)
    try:
        result = _run_on_loop(
            platform,
            lambda: fn(query=query, chat_id=chat_id, page=page, per_page=per_page))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"search_mattermost_posts failed: {e}"})
    if isinstance(result, dict):
        result.pop("raw_response", None)
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


# --- registrations -----------------------------------------------------------
# toolset "message_manage" is added to _DIRECT_SURFACE_TOOLSETS in tools/tool_search.py,
# so these are always visible (not deferred behind tool_search), like the other
# Mattermost surface toolsets.

_SHM = {
    "name": "message_manage",
    "description": (
        "Manage Mattermost messages the bot owns. The bot MUST name the exact "
        "message_id (post id) and target 'mattermost:chat_id' — there is no fallback "
        "that guesses which message. Requires the live gateway adapter."
    ),
}

registry.register(
    name="delete_message",
    toolset="message_manage",
    schema={
        "name": "delete_message",
        "description": (
            "Delete a SPECIFIC Mattermost message the bot owns (the exact post id, from "
            "conversation context). Requires target 'mattermost:chat_id' and message_id. "
            "Only the bot's own posts can be deleted; failures are reported honestly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "message_id": {"type": "string", "description": "REQUIRED. The exact post id to delete."},
            },
            "required": ["target", "message_id"],
        },
    },
    handler=lambda args, **kw: delete_message(args, **kw),
    check_fn=lambda: True,
)

registry.register(
    name="pin_message",
    toolset="message_manage",
    schema={
        "name": "pin_message",
        "description": (
            "Pin a SPECIFIC Mattermost message (the exact post id) so it shows in the channel's "
            "pinned view. Requires target 'mattermost:chat_id' and message_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "message_id": {"type": "string", "description": "REQUIRED. The exact post id to pin."},
            },
            "required": ["target", "message_id"],
        },
    },
    handler=lambda args, **kw: pin_message(args, **kw),
    check_fn=lambda: True,
)

registry.register(
    name="unpin_message",
    toolset="message_manage",
    schema={
        "name": "unpin_message",
        "description": (
            "Unpin a SPECIFIC Mattermost message (the exact post id). Requires target "
            "'mattermost:chat_id' and message_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "message_id": {"type": "string", "description": "REQUIRED. The exact post id to unpin."},
            },
            "required": ["target", "message_id"],
        },
    },
    handler=lambda args, **kw: unpin_message(args, **kw),
    check_fn=lambda: True,
)

registry.register(
    name="search_mattermost_posts",
    toolset="message_manage",
    schema={
        "name": "search_mattermost_posts",
        "description": (
            "Search Mattermost posts across the team the target conversation belongs to "
            "(the bot's own chat history). Requires target 'mattermost:chat_id' (used to "
            "resolve the team scope) and a query. Returns matching posts; use for 'what "
            "did we say about X' without resorting to session_search on a local DB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "mattermost:chat_id of the conversation."},
                "query": {"type": "string", "description": "REQUIRED. Search terms."},
                "page": {"type": "integer", "description": "0-based page (default 0)."},
                "per_page": {"type": "integer", "description": "Results per page, max 200 (default 20)."},
            },
            "required": ["target", "query"],
        },
    },
    handler=lambda args, **kw: search_mattermost_posts(args, **kw),
    check_fn=lambda: True,
)