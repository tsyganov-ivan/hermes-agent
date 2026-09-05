"""Bridge registry sync: Hermes commands -> native hermes-bridge plugin.

The Go plugin (``plugins/platforms/mattermost/bridge``) is a dumb registry
receiver: it does not know Hermes commands. This module gathers the gateway
slash commands from ``hermes_cli.commands.COMMAND_REGISTRY``, namespaces each
with a configurable prefix (default ``hermes:`` so it does not collide with
built-in Mattermost commands), and pushes the registry to the plugin's REST
endpoint ``POST /plugins/hermes-bridge/config``.

Registration happens once at gateway startup (``MattermostAdapter.connect``);
the plugin applies it and (re)registers the slash commands on the server. The
inbound ``_handle_bridge_command`` path strips the prefix back off, so the
runner dispatches ``/new`` (not ``/hermes:new``) to the existing handlers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_PREFIX = "hermes"
DEFAULT_PLUGIN_PATH = "plugins/hermes-bridge/config"


def _gateway_command_specs(prefix: str) -> List[Dict[str, Any]]:
    """Collect gateway-usable slash commands as ``CommandSpec`` payload entries.

    ``cli_only`` commands (without a ``gateway_config_gate``) and pure-CLI
    aliases are skipped; the canonical name is emitted once (aliases reuse the
    same handler via the dispatch table, so registering them adds no value and
    would only clutter the autocomplete list).
    """
    from hermes_cli.commands import COMMAND_REGISTRY  # lazy: keeps adapter import-light

    specs: List[Dict[str, Any]] = []
    for cmd in COMMAND_REGISTRY:
        if cmd.cli_only and not cmd.gateway_config_gate:
            continue
        trigger = f"{prefix}:{cmd.name}"
        hint = (cmd.args_hint or "").strip()
        specs.append({
            "trigger": trigger,
            "description": (cmd.description or "").strip(),
            "hint": hint or "",
            "autocomplete": True,
        })
    return specs


async def push_command_registry(
    *,
    base_url: str,
    shared_secret: str,
    prefix: Optional[str] = None,
    plugin_path: Optional[str] = None,
    session: Any = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Push the gateway command registry to the hermes-bridge plugin.

    ``session`` is the adapter's open aiohttp session (same event loop as the
    gateway). Returns the plugin's JSON response (``{ok, registered}`` on
    success, ``{error}`` on failure) — never raises for a non-2xx response so a
    registry-sync failure cannot block gateway startup.
    """
    import aiohttp

    prefix = (prefix or DEFAULT_COMMAND_PREFIX).strip().rstrip(":")
    specs = _gateway_command_specs(prefix)
    if not specs:
        return {"ok": True, "registered": 0, "skipped": "empty registry"}

    url = f"{base_url.rstrip('/')}/{plugin_path or DEFAULT_PLUGIN_PATH}"
    payload = {"commands": specs, "replace": True}
    headers = {"Authorization": f"Bearer {shared_secret}"}

    async def _do(sess) -> Dict[str, Any]:
        try:
            resp = await sess.post(url, json=payload, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=timeout))
            async with resp:
                body = await resp.json(content_type=None)
                if getattr(resp, "status", 200) >= 400:
                    logger.warning("Mattermost: bridge registry rejected (%s): %s", getattr(resp, "status", "?"), body)
                    return {"ok": False, "status": getattr(resp, "status", None), "error": str(body)}
            return body if isinstance(body, dict) else {"ok": True, "raw": body}
        except Exception as exc:  # noqa: BLE001 — never block startup on sync failure
            logger.warning("Mattermost: bridge registry sync failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    if session is not None and not session.closed:
        # Reuse the adapter's session (bound to the gateway loop). aiohttp spins
        # its own connector thread for a fresh session-built-per-call, which is
        # fine too — but reuse avoids churn when one is already open.
        return await _do(session)
    async with aiohttp.ClientSession() as sess:
        return await _do(sess)