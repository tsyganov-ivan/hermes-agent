"""Mattermost gateway adapter — REST API v4 + WebSocket via aiohttp (no Mattermost SDK).

Environment variables:
    MATTERMOST_URL              Server URL (e.g. https://mm.example.com)
    MATTERMOST_TOKEN            Bot token or personal-access token
    MATTERMOST_ALLOWED_USERS    Comma-separated user IDs
    MATTERMOST_HOME_CHANNEL     Channel ID for cron/notification delivery
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import unquote as _unquote
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import gateway_trust_env, BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, profile_scoped as _profile_scoped_config_load

logger = logging.getLogger(__name__)

_Metadata = Optional[Dict[str, Any]]

# Server default is 16383, but 4000 is the practical limit for readable messages.
MAX_POST_LENGTH = 4000

# Channel type codes returned by the Mattermost API ("P" private → treat as group).
_CHANNEL_TYPE_MAP = {"D": "dm", "G": "group", "P": "group", "O": "channel"}

_MATTERMOST_DISABLE_MENTIONS_PROPS = {"disable_mentions": True}

_RECONNECT_BASE_DELAY, _RECONNECT_MAX_DELAY, _RECONNECT_JITTER = 2.0, 60.0, 0.2  # exponential backoff

_POST_WITH_FILE_ERROR = "Failed to post with file"
_MEDIA_MSG_TYPES = (("image/", MessageType.PHOTO), ("audio/", MessageType.VOICE))  # first match wins
_INBOUND_CACHE_EXT = {"image/": ".png", "audio/": ".ogg"}  # mime prefix → default extension for cached media


def _with_mentions_disabled(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a post payload that prevents Mattermost from firing mentions."""
    props, disable = payload.get("props"), _MATTERMOST_DISABLE_MENTIONS_PROPS
    payload["props"] = {**props, **disable} if isinstance(props, dict) else dict(disable)
    return payload


def _channel_id_set(raw: Any) -> set:
    """Parse a list or comma-separated string of channel IDs into a stripped set."""
    items = raw if isinstance(raw, list) else str(raw).split(",")
    return {str(c).strip() for c in items if str(c).strip()}


def _csv(value: Any) -> str:
    return ",".join(str(v) for v in value) if isinstance(value, list) else str(value)

# Common unicode emoji glyphs → Mattermost emoji names. Mattermost's /reactions only accepts a
# name (thumbsup/+1), NOT a raw glyph (a "👍" 404s with "couldn't find the emoji"). Strips
# variation selectors (U+FE0F) and skin-tone modifiers before looking up, so "👍🏽" / "❤️" match.
_REACTION_EMOJI_GLYPH_TO_NAME = {
    "\U0001F44D": "+1",          # 👍 
    "\U0001F44E": "-1",          # 👎
    "\u2764": "heart",           # ❤
    "\U0001F494": "broken_heart",# 💔
    "\u2795": "heavy_plus_sign", # ➕
    "\u2796": "heavy_minus_sign",# ➖
    "\u2705": "white_check_mark",# ✅
    "\u2611": "ballot_box_with_check",  # ☑
    "\U0001F600": "grinning",    # 😀
    "\U0001F603": "smiley",      # 😃
    "\U0001F604": "smile",       # 😄
    "\U0001F60A": "blush",       # 😊
    "\U0001F60D": "heart_eyes",  # 😍
    "\U0001F602": "joy",         # 😂
    "\U0001F525": "fire",        # 🔥
    "\U0001F389": "tada",        # 🎉
    "\U0001F38A": "confetti_ball",# 🎊
    "\U0001F680": "rocket",      # 🚀
    "\U0001F44F": "clap",        # 👏
    "\U0001F450": "open_hands",  # 👐
    "\U0001F4AF": "100",         # 💯
    "\u2B50": "star",            # ⭐
    "\U0001F31F": "star2",       # 🌟
    "\U0001F31E": "sun_with_face",  # 🌞
    "\U0001F44C": "ok_hand",     # 👌
    "\U0001F64F": "pray",        # 🙏
    "\U0001F64C": "raised_hands",# 🙌
    "\U0001F918": "metal",       # 🤘
    "\U0001F596": "spock-hand",  # 🖖
    "\U0001F4A1": "bulb",        # 💡
    "\U0001F4A4": "zzz",         # 💤
    "\U0001F612": "unamused",    # 😒
    "\U0001F611": "expressionless",  # 😑
    "\U0001F614": "pensive",     # 😔
    "\U0001F62E": "open_mouth",  # 😮
    "\U0001F62D": "sob",         # 😭
    "\U0001F62A": "sleepy",      # 😪
    "\U0001F622": "cry",         # 😢
    "\U0001F61D": "stuck_out_tongue_closed_eyes",  # 😝
    "\U0001F61B": "stuck_out_tongue",  # 😛
    "\U0001F60B": "yum",         # 😋
    "\U0001F60E": "sunglasses",  # 😎
    "\U0001F913": "nerd_face",   # 🤓
    "\U0001F914": "thinking",    # 🤔
    "\U0001F4DCA": "chart_with_upwards_trend",  # 📊
    "\u2708": "airplane",        # ✈
    "\U0001F4C5": "calendar",    # 📅
    "\U0001F4E7": "e-mail",      # 📧
}


def _normalize_reaction_emoji(value: str) -> str:
    """Return the Mattermost emoji *name* for ''value''.

    Passthrough unchanged when the caller already provided a name-like token (no unicode above
    ASCII, no dashless colon wrapper). Otherwise looks up the stripped glyph in the map; falls back
    to the caller's token (trimmed of ``:...:``) when unknown so the request at least reaches the
    server instead of being silently dropped."""
    raw = (value or "").strip()
    if not raw:
        return ""
    stripped = raw.replace("\uFE0F", "")  # variation selector
    # Skin-tone modifiers (U+1F3FB..U+1F3FF)
    for cp in range(0x1F3FB, 0x1F400):
        stripped = stripped.replace(chr(cp), "")
    colon_wrapped = stripped.startswith(":") and stripped.endswith(":")
    if stripped and not colon_wrapped:
        # Any name token is already usable; glyphs are 1-2 code points and non-ASCII.
        if all(ord(ch) < 128 for ch in stripped):
            return stripped
        resolved = _REACTION_EMOJI_GLYPH_TO_NAME.get(stripped) or _REACTION_EMOJI_GLYPH_TO_NAME.get(raw)
        return resolved or stripped
    inner = stripped[1:-1] if colon_wrapped else stripped
    return inner or raw


def _post_result(data: Dict[str, Any], error: str) -> SendResult:
    if not data or "id" not in data:
        return SendResult(success=False, error=error)
    return SendResult(success=True, message_id=data["id"])


def _url_filename(url: str, fallback: str) -> str:
    return url.rsplit("/", 1)[-1].split("?")[0] or fallback


def _url_and_token(config) -> Tuple[str, str]:
    """(server URL, token): ``config`` first, MATTERMOST_URL / MATTERMOST_TOKEN env fallback."""
    extra = getattr(config, "extra", {}) or {}
    return (extra.get("url") or _get_scoped_secret("MATTERMOST_URL", ""),
            getattr(config, "token", None) or _get_scoped_secret("MATTERMOST_TOKEN", ""))


def check_mattermost_requirements() -> bool:
    """Return True if the Mattermost adapter runtime dependency is available."""
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        logger.warning("Mattermost: aiohttp not installed")
        return False


def validate_mattermost_config(config: PlatformConfig) -> bool:
    """Return True when Mattermost has enough config to connect."""
    url, token = _url_and_token(config)
    if not token.strip():
        logger.debug("Mattermost: MATTERMOST_TOKEN not set")
        return False
    if not url.strip():
        logger.warning("Mattermost: MATTERMOST_URL not set")
        return False
    return True


class MattermostAdapter(BasePlatformAdapter):
    """Gateway adapter for Mattermost (self-hosted or cloud)."""

    splits_long_messages = True  # send() chunks via truncate_message(MAX_POST_LENGTH)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.MATTERMOST)
        self._base_url, self._token = _url_and_token(config)
        self._base_url = self._base_url.rstrip("/")
        self._bot_user_id = self._bot_username = ""
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse
        self._ws_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing = False
        # Reply mode: "thread" to nest replies, "off" for flat messages.
        self._reply_mode: str = (
            config.extra.get("reply_mode", "") or _get_scoped_secret("MATTERMOST_REPLY_MODE", "off")).lower()
        # Native hermes-bridge plugin: prefix namespaces our slash commands so they
        # don't collide with built-in Mattermost commands. Command registry sync is
        # off by default; enable by setting a shared secret (MATTERMOST_BRIDGE_SECRET).
        self._bridge_prefix: str = (
            config.extra.get("bridge_command_prefix", "")
            or _get_scoped_secret("MATTERMOST_BRIDGE_COMMAND_PREFIX", "hermes:")).strip().rstrip(":")
        self._bridge_secret: str = (
            config.extra.get("bridge_shared_secret", "")
            or _get_scoped_secret("MATTERMOST_BRIDGE_SECRET", ""))
        self._bridge_plugin_path: str = (
            config.extra.get("bridge_plugin_path", "")
            or _get_scoped_secret("MATTERMOST_BRIDGE_PLUGIN_PATH",
                                  "plugins/hermes-bridge/config"))
        # Read reactions: when true, reaction_added on the bot's own posts/threads is
        # surfaced to the agent as an internal signal (no visible reply). Default off.
        _read_rx = (config.extra.get("read_reactions", "") or _get_scoped_secret("MATTERMOST_READ_REACTIONS", "false"))
        self._read_reactions: bool = str(_read_rx).strip().lower() in {"1", "true", "yes", "on"}
        # Reply-to-reaction: when read_reactions is on, false (default) stages a passive sidecar
        # note (no visible reply); true responds actively with a full agent turn in the thread.
        _reply_rx = (config.extra.get("reaction_reply", "") or _get_scoped_secret("MATTERMOST_REACTION_REPLY", "false"))
        self._reaction_reply: bool = str(_reply_rx).strip().lower() in {"1", "true", "yes", "on"}
        # Per-channel last inbound post, so react without an explicit message_id targets the
        # conversation's own most recent message instead of (incorrectly) the home channel.
        self._last_inbound_by_chat: Dict[str, str] = {}
        self._last_post_status: Optional[int] = None  # POST-only, read by the broken-thread-root fallback
        self._last_post_error: str = ""
        self._dedup = MessageDeduplicator()
        # /model interactive picker state keyed by channel_id. Holds the gateway's
        # on_model_selected closure + the provider list, so a click over the bridge
        # can drive provider -> model drill-down and then run the switch.
        self._model_picker_state: Dict[str, dict] = {}

    # --- HTTP helpers ---

    def _headers(self) -> Dict[str, str]:
        return {**self._auth_header(), "Content-Type": "application/json"}

    def _auth_header(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _api(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """{method} /api/v4/{path}; POST also records _last_post_status/_last_post_error."""
        import aiohttp
        if ".." in path:
            logger.error("MM API path traversal blocked: %s", path)
            return {}
        url = f"{self._base_url}/api/v4/{path.lstrip('/')}"
        is_post = method == "POST"
        if is_post:
            self._last_post_status, self._last_post_error = None, ""
        kwargs: Dict[str, Any] = {"headers": self._headers()}
        if payload is not None:
            kwargs["json"] = payload
        if method != "PUT":  # PUT relies on the session default timeout
            kwargs["timeout"] = aiohttp.ClientTimeout(total=30)
        try:
            async with getattr(self._session, method.lower())(url, **kwargs) as resp:
                if is_post:
                    self._last_post_status = resp.status
                if resp.status >= 400:
                    body = await resp.text()
                    if is_post:
                        self._last_post_error = body or ""
                    logger.error("MM API %s %s → %s: %s", method, path, resp.status, body[:200])
                    return {}
                return await resp.json()
        except aiohttp.ClientError as exc:
            if is_post:
                self._last_post_error = str(exc)
            logger.error("MM API %s %s network error: %s", method, path, exc)
            return {}

    async def _api_get(self, path: str) -> Dict[str, Any]:
        return await self._api("GET", path)

    async def _api_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._api("POST", path, payload)

    async def send_reaction(self, post_id: str, emoji_name: str) -> Dict[str, Any]:
        """Set an emoji reaction on ``post_id`` via POST /api/v4/reactions.

        Mattermost requires ``user_id`` (the actor) alongside ``post_id`` + ``emoji_name`` and only
        accepts the emoji's *name* (``thumbsup``/``+1``), NOT the unicode glyph (a raw ``👍`` 404s
        with "couldn't find the emoji"). ``_normalize_reaction_emoji`` maps common glyphs to their
        Mattermost names. Returns ``{"success": True/False, ...}`` (never a bare ``{}``) so callers
        can't mistake an API failure for success."""
        emoji = _normalize_reaction_emoji(emoji_name)
        if not self._bot_user_id or not post_id or not emoji:
            return {"success": False,
                    "error": f"send_reaction skipped (bot_user_id={self._bot_user_id} post={post_id} emoji={emoji_name!r})"}
        result = await self._api_post("reactions", {
            "user_id": self._bot_user_id,
            "post_id": post_id,
            "emoji_name": emoji,
        })
        if not result or "user_id" not in result:
            return {"success": False,
                    "error": "Mattermost rejected the reaction (unknown emoji name or server error). "
                             f"emoji={emoji_name!r} → normalized={emoji!r}"}
        result["success"] = True
        return result

    async def add_reaction(self, chat_id: str, message_id: str, emoji: str) -> Dict[str, Any]:
        """Adapter method consumed by send_message_tool action='react'. Reacts ONLY on the exact
        ``message_id`` given — there is deliberately no resolution fallback (reacting to a guessed
        "last message" silently misfires when the bot's own progress bubbles sit in the thread)."""
        post_id = (message_id or "").strip()
        if not post_id:
            return {"success": False,
                    "error": "add_reaction requires message_id — react to a specific post, not a guess."}
        return await self.send_reaction(post_id, emoji)

    async def remove_reaction(self, chat_id: str, message_id: str, emoji: str = "") -> Dict[str, Any]:
        """Retract the bot's reaction from ``message_id`` (DELETE /api/v4/users/{me}/posts/{id}/reactions/{emoji}).

        A successful Mattermost reaction DELETE returns an empty 200 body; the shared ``_api``
        helper maps that to ``{}``. So success == an empty dict here. ``emoji`` is optional because
        a bare collection delete is accepted by some servers, but passing the exact name is the
        reliable form."""
        if not self._bot_user_id or not message_id:
            return {"success": False, "error": "remove_reaction skipped (no bot id or message id)"}
        emoji_name = _normalize_reaction_emoji(emoji)
        path = f"users/{self._bot_user_id}/posts/{message_id}/reactions"
        if emoji_name:
            path += f"/{emoji_name}"
        result = await self._api("DELETE", path)
        # DELETE success returns {} (empty body); the _api helper also returns {} on a >=400 error,
        # but _last_post_status/_last_post_error are only tracked for POST. Reply success:false on
        # any non-4xx-tolerant signal so the agent doesn't claim victory after a 404.
        if result is not None and result:
            result["success"] = True
            return result
        if self._last_post_status is not None and self._last_post_status >= 400:
            return {"success": False, "error": self._last_post_error or f"HTTP {self._last_post_status}"}
        return {"success": True}

    def _last_post_failure_is_broken_thread_root(self) -> bool:
        """Return True only for clear invalid/missing Mattermost thread roots."""
        body = (self._last_post_error or "").lower()
        if self._last_post_status not in {400, 404} or not body:
            return False
        return (any(marker in body for marker in ("root_id", "rootid", "root id", "thread", "post"))
                and any(marker in body for marker in ("invalid", "not found", "does not exist", "missing")))

    async def _post_preserving_thread(
        self, chat_id: str, payload: Dict[str, Any], metadata: _Metadata) -> Dict[str, Any]:
        """Post once, optionally falling back flat for final notify content."""
        data = await self._api_post("posts", payload)
        if (data or "root_id" not in payload or not (isinstance(metadata, dict) and metadata.get("notify"))
                or not self._last_post_failure_is_broken_thread_root()):
            return data
        flat_payload = {k: v for k, v in payload.items() if k != "root_id"}
        flat_payload["message"] = ("⚠️ Mattermost thread delivery failed; posting final reply in channel.\n\n"
                                   + str(flat_payload.get("message") or "")).strip()
        logger.warning("Mattermost: falling back to flat channel delivery for notify-worthy post in %s", chat_id)
        return await self._api_post("posts", flat_payload)

    async def _post_message(self, chat_id: str, message: str, reply_to: Optional[str], metadata: _Metadata,
                            file_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Build a mentions-disabled post payload (+ optional root_id) and post it."""
        base: Dict[str, Any] = {"channel_id": chat_id, "message": message}
        if file_ids is not None:
            base["file_ids"] = file_ids
        payload = _with_mentions_disabled(base)
        if self._reply_mode == "thread":
            # root_id from reply_to, else metadata["thread_id"]/["root_id"], resolved to the true thread root.
            candidate = reply_to or (
                isinstance(metadata, dict) and (metadata.get("thread_id") or metadata.get("root_id")))
            if candidate:
                payload["root_id"] = await self._resolve_root_id(str(candidate))
        return await self._post_preserving_thread(chat_id, payload, metadata)

    async def send_interactive(
            self, chat_id: str, text: str, buttons: Optional[List[Dict[str, Any]]] = None,
            menu: Optional[Dict[str, Any]] = None, *,
            reply_to: Optional[str] = None,
            question_id: Optional[str] = None,
            metadata: _Metadata = None) -> SendResult:
        """Send a post with interactive message buttons and/or a select menu.

        All MM transport detail is hidden: the bot never sees integration URLs or
        callback payloads. ``buttons`` = [{id, label, style}]; ``menu`` =
        {id, name, placeholder, options: [{label, value}]}. Clicks arrive back as
        ``hermes_bridge_interact`` WS events (relayed by the native plugin).
        """
        if not buttons and not menu:
            return SendResult(success=False, error="send_interactive requires buttons or menu")
        actions: List[Dict[str, Any]] = []
        for b in buttons or []:
            bid = str(b.get("id") or "").strip()
            if not bid:
                continue
            blabel = str(b.get("label") or bid)
            ctx = {"action_id": bid, "label": blabel}
            if question_id:
                ctx["question_id"] = question_id
            actions.append({
                "id": bid, "type": "button", "name": blabel,
                "style": str(b.get("style") or "default"),
                "integration": {"url": "/plugins/hermes-bridge/interact", "context": ctx},
            })
        if menu:
            mid = str(menu.get("id") or "").strip()
            if mid:
                mctx = {"action_id": mid}
                if question_id:
                    mctx["question_id"] = question_id
                actions.append({
                    "id": mid, "type": "select", "name": str(menu.get("placeholder") or menu.get("name") or mid),
                    "data_source": str(menu.get("data_source") or ""),
                    "options": [
                        {"text": str(o.get("label") or o.get("value")), "value": str(o.get("value") or "")}
                        for o in (menu.get("options") or [])],
                    "integration": {"url": "/plugins/hermes-bridge/interact", "context": mctx},
                })
        if not actions:
            return SendResult(success=False, error="send_interactive: no valid actions")
        base: Dict[str, Any] = {"channel_id": chat_id, "message": ""}
        payload = _with_mentions_disabled(base)
        payload["props"] = {**(payload.get("props") or {}),
                            "attachments": [{"text": text, "actions": actions}]}
        if reply_to:
            payload["root_id"] = await self._resolve_root_id(reply_to)
        result = await self._api_post("posts", payload)
        return _post_result(result, "Failed to create interactive post")

    async def send_ephemeral(self, chat_id: str, user_id: str, text: str, *,
                             metadata: _Metadata = None) -> SendResult:
        """Send a message visible only to ``user_id`` in ``chat_id`` (POST /posts/ephemeral)."""
        import aiohttp
        payload = {
            "user_id": user_id,
            "post": {"channel_id": chat_id, "message": self.format_message(text), "props": _MATTERMOST_DISABLE_MENTIONS_PROPS},
        }
        if self._reply_mode == "thread" and isinstance(metadata, dict) and metadata.get("thread_id"):
            payload["post"]["root_id"] = str(metadata["thread_id"])
        url = f"{self._base_url}/api/v4/posts/ephemeral"
        try:
            async with self._session.post(url, json=payload, headers=self._headers(),
                                          timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error("MM ephemeral → %s: %s", resp.status, body[:200])
                    return SendResult(success=False, error=f"ephemeral post failed ({resp.status})")
                return SendResult(success=True)
        except aiohttp.ClientError as exc:
            logger.error("MM ephemeral network error: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def _post_with_file(self, chat_id: str, file_id: str, caption: Optional[str], reply_to: Optional[str],
                              metadata: _Metadata) -> SendResult:
        return _post_result(await self._post_message(chat_id, caption or "", reply_to, metadata, [file_id]),
                            _POST_WITH_FILE_ERROR)

    async def _upload_file(self, channel_id: str, file_data: bytes, filename: str,
                           content_type: str = "application/octet-stream") -> Optional[str]:
        """Upload a file and return its file ID, or None on failure."""
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("channel_id", channel_id)
        form.add_field("files", file_data, filename=filename, content_type=content_type)
        async with self._session.post(f"{self._base_url}/api/v4/files", headers=self._auth_header(), data=form,
                                      timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.error("MM file upload → %s: %s", resp.status, body[:200])
                return None
            infos = (await resp.json()).get("file_infos", [])
            return infos[0]["id"] if infos else None

    # --- Required overrides ---

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Mattermost and start the WebSocket listener."""
        import aiohttp
        if not self._base_url or not self._token:
            logger.error("Mattermost: URL or token not configured")
            return False
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=gateway_trust_env())
        self._closing = False
        me = await self._api_get("users/me")
        if not me or "id" not in me:
            logger.error("Mattermost: failed to authenticate — check MATTERMOST_TOKEN and MATTERMOST_URL")
            await self._session.close()
            return False
        self._bot_user_id, self._bot_username = me["id"], me.get("username", "")
        logger.info(
            "Mattermost: authenticated as @%s (%s) on %s", self._bot_username, self._bot_user_id, self._base_url)
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._mark_connected()
        self._wire_plugin_handlers(None)  # plugin-registered native handlers
        if not is_reconnect and self._bridge_secret:
            # Best-effort: push the gateway command registry to the native plugin.
            # Never blocks startup — failures only log (bridge_registry never raises).
            try:
                await self._sync_bridge_registry()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Mattermost: bridge registry sync errored: %s", exc)
        return True

    async def disconnect(self) -> None:
        self._closing = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("Mattermost: disconnected")

    async def _resolve_root_id(self, post_id: str) -> str:
        """Resolve a post_id to its thread root_id (a reply's own ID causes "Invalid RootId parameter")."""
        if not post_id:
            return post_id
        data = await self._api_get(f"posts/{post_id}")
        return data["root_id"] if data and data.get("root_id") else post_id

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        """Send a message (or multiple chunks) to a channel; reply_to / metadata["thread_id"] is the root post."""
        if not content:
            return SendResult(success=True)
        result = SendResult(success=True)
        for chunk in self.truncate_message(self.format_message(content), MAX_POST_LENGTH):
            result = _post_result(await self._post_message(chat_id, chunk, reply_to, metadata), "Failed to create post")
            if not result.success:
                break
        return result

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        data = await self._api_get(f"channels/{chat_id}")
        if not data:
            return {"name": chat_id, "type": "channel"}
        return {"name": data.get("display_name") or data.get("name") or chat_id,
                "type": _CHANNEL_TYPE_MAP.get(data.get("type", "O"), "channel")}

    # --- Optional overrides ---

    async def send_typing(self, chat_id: str, metadata: _Metadata = None) -> None:
        await self._api_post(f"users/{self._bot_user_id}/typing", {"channel_id": chat_id})

    async def edit_message(self, chat_id: str, message_id: str, content: str, *, finalize: bool = False) -> SendResult:
        payload = _with_mentions_disabled({"message": self.format_message(content)})
        return _post_result(await self._api("PUT", f"posts/{message_id}/patch", payload), "Failed to edit post")

    # --- /model interactive picker (gateway-drive, mirrors Telegram's send_model_picker) ---

    # Action_id namespace for the model picker, intercepted in _handle_bridge_interact BEFORE
    # the generic interactive->TEXT path. Keep these ALNUMS ONLY: Mattermost's action ids may
    # consist of letters and numbers, no other characters (documented for interactive actions).
    _PICKER_ACTION_PROVIDER = "hmppro"     # select menu -> selected_option = provider slug
    _PICKER_ACTION_MODEL = "hmpmod"        # select menu -> selected_option = model id
    _PICKER_ACTION_CANCEL = "hmpcan"       # button -> abort the picker

    async def send_model_picker(
            self, chat_id: str, providers: list, current_model: str, current_provider: str,
            session_key: str, on_model_selected, metadata: _Metadata = None) -> SendResult:
        """Send an interactive two-step model picker (provider menu -> model menu).

        The gateway's ``_handle_model_command`` calls this (keyword args) when the platform
        supports it; the whole switch + persist logic lives in the ``on_model_selected``
        closure it passes in — this adapter only renders the menus and relays the pick. The
        same post is PUT-updated in place on each step (like Telegram editing its picker).
        """
        if self._session is None:
            return SendResult(success=False, error="Not connected")
        provider_options = []
        for p in providers or []:
            slug = str(p.get("slug") or "").strip()
            if not slug:
                continue
            label = str(p.get("name") or slug)
            if slug == current_provider:
                label = f"✓ {label}"
            provider_options.append({"text": label, "value": slug})
        if not provider_options:
            return SendResult(success=False, error="No providers")
        try:
            menu = {
                "id": self._PICKER_ACTION_PROVIDER, "name": "Провайдер",
                "placeholder": "Провайдер",
                "options": provider_options,
            }
            actions = self._picker_actions([menu], cancel=True)
            text = self.format_message(
                f"⚙ **Model Configuration**\n\nCurrent model: `{current_model or 'unknown'}`\n"
                f"Provider: {current_provider}\n\nSelect a provider:")
            thread_id = (metadata or {}).get("thread_id") if isinstance(metadata, dict) else None
            root_id = await self._resolve_root_id(thread_id) if thread_id else None
            payload = _with_mentions_disabled({"channel_id": chat_id, "message": "",
                                               "props": {"attachments": [{"text": text, "actions": actions}]}})
            if root_id:
                payload["root_id"] = root_id
            data = await self._api_post("posts", payload)
            if not data or "id" not in data:
                return SendResult(success=False, error="Failed to create model picker post")
            self._model_picker_state[str(chat_id)] = {
                "msg_id": data["id"], "providers": providers or [], "session_key": session_key,
                "on_model_selected": on_model_selected, "current_model": current_model,
                "current_provider": current_provider, "selected_provider": None,
                "selected_provider_name": None,
            }
            return SendResult(success=True, message_id=data["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] send_model_picker failed: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))

    def _picker_actions(self, menus: List[Dict[str, Any]], *, cancel: bool = False) -> List[Dict[str, Any]]:
        """Build MM actions for the picker: the given select menus + optional Cancel button."""
        actions = []
        interact_url = self._bridge_interact_url()
        for m in menus:
            mid = str(m.get("id") or "").strip()
            if not mid or not m.get("options"):
                continue
            actions.append({
                "id": mid, "type": "select", "name": str(m.get("placeholder") or m.get("name") or mid),
                "data_source": "",
                "options": [{"text": str(o["text"]), "value": str(o["value"])} for o in m["options"]],
                "integration": {"url": interact_url, "context": {"action_id": mid}},
            })
        if cancel:
            actions.append({
                "id": self._PICKER_ACTION_CANCEL, "type": "button", "name": "Отмена",
                "style": "default",
                "integration": {"url": interact_url,
                                "context": {"action_id": self._PICKER_ACTION_CANCEL}},
            })
        return actions

    def _bridge_interact_url(self) -> str:
        """Relative interact URL for the native bridge plugin (e.g. /plugins/hermes-bridge/interact).

        Derives from the configured ``bridge_plugin_path`` (which points at the config endpoint,
        ``plugins/hermes-bridge/config``) so it tracks the plugin id instead of a hardcoded string.
        """
        parts = str(self._bridge_plugin_path or "").strip("/").split("/")
        if parts and parts[-1] == "config":
            parts = parts[:-1]
        return f"/{'/'.join(parts)}/interact" if parts else "/plugins/hermes-bridge/interact"

    async def _update_picker_post(self, post_id: str, text: str, actions: Optional[List[Dict[str, Any]]]) -> None:
        """PUT-patch the picker post to a new step (attachments text + optional actions)."""
        attrs: Dict[str, Any] = {"text": text}
        if actions is not None:
            attrs["actions"] = actions
        payload = _with_mentions_disabled({"message": "",
                                           "props": {"attachments": [attrs]}})
        try:
            await self._api("PUT", f"posts/{post_id}/patch", payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] picker post update failed: %s", self.name, exc)

    async def _handle_model_picker_callback(self, data: Dict[str, Any]) -> None:
        """Route a bridge interact callback into the /model picker state machine."""
        channel_id = str(data.get("channel_id") or "").strip()
        post_id = str(data.get("post_id") or "").strip()
        action_id = str(data.get("action_id") or "").strip()
        selected = str(data.get("selected_option") or "").strip()
        state = self._model_picker_state.get(channel_id)
        if not state:
            return  # picker already closed/expired — drop the stray click
        try:
            from hermes_cli.providers import get_label
        except ImportError:
            get_label = lambda slug: slug  # noqa: E731
        if action_id == self._PICKER_ACTION_CANCEL:
            self._model_picker_state.pop(channel_id, None)
            await self._update_picker_post(post_id, "⚙ Model picker cancelled.", [])
            return
        if action_id == self._PICKER_ACTION_PROVIDER and selected:
            provider = next((p for p in state["providers"] if str(p.get("slug")) == selected), None)
            if not provider:
                await self._update_picker_post(post_id, "❌ Provider not found.", [])
                self._model_picker_state.pop(channel_id, None)
                return
            models = provider.get("models") or []
            total = provider.get("total_models") or len(models)
            shown = models[:50]
            opts = []
            for m in shown:
                label = str(m).rsplit("/", 1)[-1] if "/" in str(m) else str(m)
                if m == state.get("current_model"):
                    label = f"✓ {label}"
                opts.append({"text": label, "value": str(m)})
            if not opts:
                await self._update_picker_post(post_id, "❌ Provider has no pickable models.", [])
                self._model_picker_state.pop(channel_id, None)
                return
            state["selected_provider"] = selected
            state["selected_provider_name"] = str(provider.get("name") or get_label(selected) or selected)
            menu = {"id": self._PICKER_ACTION_MODEL, "name": "Модель", "placeholder": "Модель", "options": opts}
            extra = f"\n_{total - len(shown)} more — type `/model <name>` directly_" if total > len(shown) else ""
            await self._update_picker_post(
                post_id,
                self.format_message(f"⚙ **Model Configuration**\n\nProvider: **{state['selected_provider_name']}**\nSelect a model:{extra}"),
                self._picker_actions([menu], cancel=True))
            logger.info("[%s] model picker: provider %s selected, %d models", self.name, selected, len(shown))
            return
        if action_id == self._PICKER_ACTION_MODEL and selected:
            cb = state.get("on_model_selected")
            provider_slug = state.get("selected_provider") or state.get("current_provider") or ""
            self._model_picker_state.pop(channel_id, None)
            if not cb:
                await self._update_picker_post(post_id, "❌ Picker expired — use /model again.", [])
                return
            try:
                result_text = await cb(channel_id, selected, provider_slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] model picker switch failed: %s", self.name, exc)
                result_text = f"❌ Model switch failed ({exc})."
            await self._update_picker_post(post_id, self.format_message(result_text), [])
            logger.info("[%s] model picker: model %s chosen (provider %s)", self.name, selected, provider_slug)
            return

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        return await self._send_url_as_file(chat_id, image_url, caption, reply_to, "image", metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        return await self._send_local_file(chat_id, image_path, caption, reply_to, metadata=metadata)

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        return await self._send_local_file(chat_id, file_path, caption, reply_to, file_name, metadata)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        return await self._send_local_file(chat_id, audio_path, caption, reply_to, metadata=metadata)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        return await self._send_local_file(chat_id, video_path, caption, reply_to, metadata=metadata)

    def format_message(self, content: str) -> str:
        """Mattermost renders standard Markdown; reduce ![alt](url) to the bare URL (inline preview)."""
        return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)

    # --- File helpers ---

    async def _send_url_as_file(self, chat_id: str, url: str, caption: Optional[str], reply_to: Optional[str],
                                kind: str = "file", metadata: _Metadata = None) -> SendResult:
        """Download a URL and upload it as a file attachment (text fallback with the URL on failure)."""
        from tools.url_safety import is_safe_url

        async def fallback() -> SendResult:
            return await self.send(chat_id, f"{caption or ''}\n{url}".strip(), reply_to, metadata=metadata)

        if not is_safe_url(url):
            logger.warning("Mattermost: blocked unsafe URL (SSRF protection)")
            return await fallback()
        import aiohttp
        for attempt in range(3):  # retry 5xx/429 and network errors twice with linear backoff
            try:
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if (resp.status >= 500 or resp.status == 429) and attempt < 2:
                        logger.debug("Mattermost download retry %d/2 for %s (status %d)",
                                     attempt + 1, url[:80], resp.status)
                    elif resp.status >= 400:
                        return await fallback()
                    else:
                        file_data, ct = await resp.read(), resp.content_type or "application/octet-stream"
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 2:
                    logger.warning("Mattermost: failed to download %s after %d attempts: %s", url, attempt + 1, exc)
                    return await fallback()
            await asyncio.sleep(1.5 * (attempt + 1))
        file_id = await self._upload_file(chat_id, file_data, _url_filename(url, f"{kind}.png"), ct)
        return await self._post_with_file(chat_id, file_id, caption, reply_to, metadata) if file_id else await fallback()

    async def _send_local_file(
        self, chat_id: str, file_path: str, caption: Optional[str], reply_to: Optional[str],
        file_name: Optional[str] = None, metadata: _Metadata = None) -> SendResult:
        """Upload a local file and attach it to a post."""
        p = Path(file_path)
        if not p.exists():
            logger.warning("Mattermost: local file not found, skipping: %s", file_path)
            return SendResult(success=True, message_id=None)
        fname = file_name or p.name
        file_id = await self._upload_file(chat_id, p.read_bytes(), fname,
                                          mimetypes.guess_type(fname)[0] or "application/octet-stream")
        if not file_id:
            return SendResult(success=False, error="File upload failed")
        return await self._post_with_file(chat_id, file_id, caption, reply_to, metadata)

    async def _load_batch_image(self, image_url: str, index: int) -> Optional[Tuple[bytes, str, str]]:
        """Read a file:// or remote image for a batch post → (data, filename, content_type), or None to skip."""
        import aiohttp
        if image_url.startswith("file://"):
            local_path = _unquote(image_url[7:])
            p = Path(local_path)
            if not p.exists():
                logger.warning("Mattermost: skipping missing image %s", local_path)
                return None
            return p.read_bytes(), p.name, mimetypes.guess_type(p.name)[0] or "image/png"
        from tools.url_safety import is_safe_url
        if not is_safe_url(image_url):
            logger.warning("Mattermost: blocked unsafe image URL in batch")
            return None
        try:
            async with self._session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status >= 400:
                    logger.warning("Mattermost: failed to download image (HTTP %d): %s", resp.status, image_url[:80])
                    return None
                file_data, ct = await resp.read(), resp.content_type or "image/png"
        except Exception as dl_err:
            logger.warning("Mattermost: download failed for %s: %s", image_url[:80], dl_err)
            return None
        return file_data, _url_filename(image_url, f"image_{index}.png"), ct

    async def send_multiple_images(self, chat_id: str, images: List[Tuple[str, str]],
                                   metadata: _Metadata = None, human_delay: float = 0.0) -> None:
        """Send a batch of images as one post; chunked at Mattermost's 5-``file_ids`` cap, each chunk
        falling back to the base per-image loop on failure."""
        if not images:
            return
        chunks = [images[i:i + 5] for i in range(0, len(images), 5)]  # Mattermost post file_ids cap
        for chunk_idx, chunk in enumerate(chunks):
            if human_delay > 0 and chunk_idx > 0:
                await asyncio.sleep(human_delay)
            file_ids, caption_parts = [], []
            try:
                for image_url, alt_text in chunk:
                    if alt_text:
                        caption_parts.append(alt_text)
                    loaded = await self._load_batch_image(image_url, len(file_ids))
                    if loaded is not None and (fid := await self._upload_file(chat_id, *loaded)):
                        file_ids.append(fid)
                if not file_ids:
                    continue
                logger.info("Mattermost: sending %d image(s) as single post (chunk %d/%d)",
                            len(file_ids), chunk_idx + 1, len(chunks))
                data = await self._post_message(chat_id, "\n".join(caption_parts), None, metadata, file_ids)
                if not data or "id" not in data:
                    logger.warning("Mattermost: multi-image post failed, falling back")
                    await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)
            except Exception as e:
                logger.warning("Mattermost: multi-image send failed (chunk %d/%d), falling back: %s",
                               chunk_idx + 1, len(chunks), e, exc_info=True)
                await super().send_multiple_images(chat_id, chunk, metadata, human_delay=human_delay)

    # --- WebSocket ---

    async def _ws_loop(self) -> None:
        """Connect to the WebSocket and listen for events, reconnecting on failure."""
        import aiohttp
        import random
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            try:
                await self._ws_connect_and_listen()
                delay = _RECONNECT_BASE_DELAY  # clean disconnect — reset backoff
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._closing:
                    return
                # Permanent auth failure: escalate via the fatal-error hook (a bare return leaves is_connected()
                # healthy with a dead listener). Type-based: substring "401" matching misclassified transient errors.
                if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in {401, 403}:
                    logger.error("Mattermost WS auth failed (HTTP %d) — stopping reconnect", exc.status)
                    # Escalate through the fatal-error hook instead of a bare return: the old silent exit
                    # left _running True, so is_connected() kept reporting healthy while the listener was
                    # dead and the gateway was never told (OOF-156 class). Type-based only — the substring
                    # fallback that used to sit below this branch misclassified transient errors whose
                    # message merely contained "401" (#80489).
                    self._set_fatal_error(
                        "mattermost_auth_error",
                        f"Mattermost WebSocket authentication rejected (HTTP {exc.status}). The bot token is "
                        "invalid, revoked, or lacks permission — check MATTERMOST_TOKEN and the bot account in "
                        "the System Console.", retryable=False)
                    await self._notify_fatal_error()
                    return
                logger.warning("Mattermost WS error: %s — reconnecting in %.0fs", exc, delay)
            if self._closing:
                return
            await asyncio.sleep(delay + delay * _RECONNECT_JITTER * random.random())
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _ws_connect_and_listen(self) -> None:
        """Single WebSocket session: connect, authenticate, process events."""
        ws_url = re.sub(r"^http", "ws", self._base_url) + "/api/v4/websocket"  # https→wss, http→ws
        logger.info("Mattermost: connecting to %s", ws_url)
        self._ws = await self._session.ws_connect(ws_url, heartbeat=30.0)
        await self._ws.send_json({"seq": 1, "action": "authentication_challenge", "data": {"token": self._token}})
        logger.info("Mattermost: WebSocket connected and authenticated")

        async for raw_msg in self._ws:
            if self._closing:
                return
            kind = raw_msg.type
            if kind in {kind.TEXT, kind.BINARY}:
                try:
                    event = json.loads(raw_msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_ws_event(event)
            elif kind in {kind.ERROR, kind.CLOSE, kind.CLOSING, kind.CLOSED}:
                logger.info("Mattermost: WebSocket closed (%s)", kind)
                break

    def _extra_or_env(self, key: str, env: str, default: str = "") -> Any:
        """config.yaml ``mattermost.<key>`` (PlatformConfig.extra) first, env var fallback."""
        raw = self.config.extra.get(key) if self.config.extra else None
        return _get_scoped_secret(env, default) if raw is None else raw

    def _apply_channel_gating(self, channel_id: str, message_text: str) -> Optional[str]:
        """Mention-gate a non-DM post; return the cleaned text, or None to ignore it. allowed_channels is a
        whitelist checked first (@mentions elsewhere are ignored); require_mention (default true) is
        bypassed in free_response_channels."""
        allowed_channels = _channel_id_set(self._extra_or_env("allowed_channels", "MATTERMOST_ALLOWED_CHANNELS"))
        if allowed_channels and channel_id not in allowed_channels:
            logger.debug("Mattermost: ignoring message in non-allowed channel: %s", channel_id)
            return None
        require_mention = str(self._extra_or_env("require_mention", "MATTERMOST_REQUIRE_MENTION", "true")
                              ).lower() not in {"false", "0", "no"}
        free_channels = _channel_id_set(
            self._extra_or_env("free_response_channels", "MATTERMOST_FREE_RESPONSE_CHANNELS"))
        mention_patterns = [f"@{self._bot_username}", f"@{self._bot_user_id}"]
        has_mention = any(pattern.lower() in message_text.lower() for pattern in mention_patterns)
        if require_mention and channel_id not in free_channels and not has_mention:
            logger.debug("Mattermost: skipping non-DM message without @mention (channel=%s)", channel_id)
            return None
        if has_mention:  # strip the @mention so the agent sees clean input
            for pattern in mention_patterns:
                message_text = re.sub(re.escape(pattern), "", message_text, flags=re.IGNORECASE).strip()
        return message_text

    async def _download_attachments(self, file_ids: List[str]) -> Tuple[List[str], List[str]]:
        """Download attachments now (URLs need auth headers downstream tools lack) → (paths, mime types)."""
        import aiohttp
        from gateway.platforms.base import (
            cache_audio_from_bytes_async,
            cache_document_from_bytes_async,
            cache_image_from_bytes_async,
        )
        media_urls, media_types = [], []
        cache_fns = {"image/": cache_image_from_bytes_async, "audio/": cache_audio_from_bytes_async}
        for fid in file_ids:
            try:
                file_info = await self._api_get(f"files/{fid}/info")
                fname = file_info.get("name", f"file_{fid}")
                mime = file_info.get("mime_type", "application/octet-stream")
                async with self._session.get(
                    f"{self._base_url}/api/v4/files/{fid}", headers=self._auth_header(),
                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status >= 400:
                        logger.warning("Mattermost: failed to download file %s: HTTP %s", fid, resp.status)
                        continue
                    file_data = await resp.read()
                    prefix = next((p for p in cache_fns if mime.startswith(p)), None)
                    if prefix:
                        media_urls.append(
                            await cache_fns[prefix](file_data, Path(fname).suffix or _INBOUND_CACHE_EXT[prefix]))
                    else:
                        media_urls.append(await cache_document_from_bytes_async(file_data, fname))
                    media_types.append(mime)
            except Exception as exc:
                logger.warning("Mattermost: error downloading file %s: %s", fid, exc)
        return media_urls, media_types

    async def _reaction_on_own_content(self, post_id: str) -> Optional[Tuple[str, Optional[str]]]:
        """Return (channel_id, thread_root_id) when the reacted post is the bot's own post or lives
        in a thread the bot started; else None. Resolves the author via the API (never trusts the
        WS event, which only carries the reacted post id). thread_root_id is the post's root_id (or
        None for a top-level own post). Own-post author == bot; an own thread means root author == bot."""
        post = await self._api_get(f"posts/{post_id}")
        if not post or not post.get("id"):
            return None
        post_user = (post.get("user_id") or "").strip()
        root_id = (post.get("root_id") or "").strip() or None
        if post_user == self._bot_user_id:
            return (post.get("channel_id", ""), root_id)
        if root_id and root_id != post_id:
            root = await self._api_get(f"posts/{root_id}")
            if root and (root.get("user_id") or "").strip() == self._bot_user_id:
                return (post.get("channel_id", ""), root_id)
        return None

    async def _channel_type_code(self, channel_id: str) -> str:
        """Resolve the real Mattermost channel type (D/G/O) via the API.

        ``reaction_added`` WS events do NOT carry ``channel_type`` (unlike ``posted``), so a
        reaction on a DM would otherwise be staged under a ``channel`` session key that never
        matches the running DM conversation. ``GET /channels/{id}`` returns ``type`` = D/G/O."""
        if channel_id:
            ch = await self._api_get(f"channels/{channel_id}")
            code = (ch or {}).get("type", "")
            if code in {"D", "G", "O", "P"}:
                return code
        return "O"

    async def _sync_bridge_registry(self) -> None:
        """Push the gateway slash-command registry to the native hermes-bridge plugin."""
        if not self._bridge_secret:
            return
        from plugins.platforms.mattermost import bridge_registry
        result = await bridge_registry.push_command_registry(
            base_url=self._base_url, shared_secret=self._bridge_secret,
            prefix=self._bridge_prefix, plugin_path=self._bridge_plugin_path,
            session=self._session)
        registered = result.get("registered", "?")
        logger.info("Mattermost: bridge registry sync -> registered %s (%s)",
                    registered, "ok" if result.get("ok") else result.get("error", "unknown"))

    async def _handle_reaction_event(self, data: Dict[str, Any]) -> None:
        """Surface a `reaction_added` on the bot's own content to the agent as an internal signal.

        Injected via MessageEvent(internal=True) so it lands in the thread's session as context the
        model can adapt to — no separate visible reply is produced. Dropped unless the reacted post
        is the bot's own or sits in a thread the bot owns (Ivan's filter), and never for the bot's
        own reactions."""
        import json as _json
        # reaction_added data can carry the reaction object either inline or JSON-encoded.
        raw_reaction = data.get("reaction")
        if isinstance(raw_reaction, str):
            try:
                reaction = _json.loads(raw_reaction)
            except (ValueError, TypeError):
                reaction = {}
        elif isinstance(raw_reaction, dict):
            reaction = raw_reaction
        else:
            reaction = {}
        emitter = str(reaction.get("user_id") or data.get("user_id") or "").strip()
        post_id = str(reaction.get("post_id") or data.get("post_id") or "").strip()
        emoji = str(reaction.get("emoji_name") or data.get("emoji_name") or "").strip()
        channel_id = str(reaction.get("channel_id") or data.get("channel_id") or "").strip()
        if self._bot_user_id and emitter == self._bot_user_id:
            return  # never react to the bot's own reactions (feedback loops)
        if not post_id or not emoji:
            return
        own = await self._reaction_on_own_content(post_id)
        if not own:
            logger.debug("Mattermost: ignoring reaction %s on non-owned post %s", emoji, post_id)
            return
        channel_id = channel_id or own[0]
        thread_root = own[1]
        channel_code = await self._channel_type_code(channel_id)
        # Same thread resolution as the `posted` path so the note lands in the right session.
        thread_id = thread_root
        if not thread_id and self._reply_mode == "thread" and channel_id:
            thread_id = post_id
        source = self.build_source(
            chat_id=channel_id,
            chat_type=_CHANNEL_TYPE_MAP.get(channel_code, "channel"),
            user_id=emitter, user_name=(data.get("user_name") or "").lstrip("@") or emitter,
            thread_id=thread_id, message_id=post_id)
        note = f"[Reaction] {source.user_name or emitter} reacted {emoji}" \
               + (" → твой пост" if not thread_root else " → в твоём треде") + "."
        logger.info("Mattermost: reaction signal %s %s → %s", emitter, emoji, post_id)
        from gateway.platforms.base import MessageEvent, MessageType
        staged = MessageEvent(
            text=note, message_type=MessageType.TEXT, source=source, internal=True, message_id=post_id)
        if self._reaction_reply:
            # Active mode: surface the reaction as a full internal turn — the agent may reply.
            await self.handle_message(staged)
            return
        # Passive mode (default): stage the note as a turn sidecar so it rides the NEXT real
        # user message (via api_content), reaching the model without spawning its own reply.
        # Key must be computed EXACTLY like the consuming turn does (runner._session_key_for_source),
        # not the adapter-level _event_session_key, or the staged note lands under a different key.
        runner = getattr(self, "gateway_runner", None)
        _key_fn = getattr(runner, "_session_key_for_source", None)
        if callable(_key_fn):
            session_key = _key_fn(source)
        else:
            session_key = self._event_session_key(staged)
        _set_notes = getattr(runner, "_set_pending_turn_sidecar_notes", None)
        if callable(_set_notes):
            _set_notes(session_key, [note])
        else:
            logger.debug("Mattermost: no runner to stage reaction sidecar note (session=%s)", session_key)

    async def _handle_bridge_command(self, data: Dict[str, Any]) -> None:
        """Handle a `hermes_bridge_command` WS event from the native plugin.

        The plugin relays a user-invoked slash command as a custom WS event
        (addressable to this bot via broadcast.UserId). Build a real
        ``MessageEvent.COMMAND`` (text ``/trigger args``) and feed it through the
        same ``handle_message`` path as a typed post, so the existing slash
        dispatcher owns routing and guard bypass for control commands.
        """
        from gateway.platforms.base import MessageEvent, MessageType
        trigger = str(data.get("trigger") or "").strip().lstrip("/")
        # Strip the configured namespace prefix (e.g. "hermes:new" -> "new") so the
        # dispatcher sees the canonical command name. If no prefix matches, keep as-is.
        if self._bridge_prefix:
            _pref = f"{self._bridge_prefix}:"
            if trigger.startswith(_pref):
                trigger = trigger[len(_pref):]
        args = str(data.get("args") or "").strip()
        channel_id = str(data.get("channel_id") or "").strip()
        channel_code = str(data.get("channel_type") or "") or await self._channel_type_code(channel_id)
        user_id = str(data.get("user_id") or "").strip()
        user_name = str(data.get("user_name") or "").lstrip("@") or user_id
        thread_id = str(data.get("thread_id") or "").strip() or None
        post_id = str(data.get("post_id") or "").strip() or None
        if not trigger or not channel_id:
            logger.warning("Mattermost: hermes_bridge_command missing trigger/channel: %s", data)
            return
        source = self.build_source(
            chat_id=channel_id,
            chat_type=_CHANNEL_TYPE_MAP.get(channel_code, "channel"),
            user_id=user_id, user_name=user_name,
            thread_id=thread_id, message_id=post_id)
        text = f"/{trigger} {args}" if args else f"/{trigger}"
        logger.info("Mattermost: bridge command /%s from %s in %s", trigger, user_name, channel_id)
        await self.handle_message(MessageEvent(
            text=text, message_type=MessageType.COMMAND, source=source, message_id=post_id))

    async def _handle_bridge_interact(self, data: Dict[str, Any]) -> None:
        """Handle a `hermes_bridge_interact` WS event from the native plugin.

        The plugin relays an interactive callback (button/menu) as a structured
        WS event (action_id, selected_option, context). Build a MessageEvent so
        the agent sees the user's choice. Both button clicks and menu selections
        surface as ordinary TEXT choices (the label/value the user picked), with
        the action identity in ``raw_message`` — never as an agent slash command.
        """
        from gateway.platforms.base import MessageEvent, MessageType
        post_id = str(data.get("post_id") or "").strip()
        action_id = str(data.get("action_id") or "").strip()
        selected = str(data.get("selected_option") or "").strip()
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        channel_id = str(data.get("channel_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        user_name = str(data.get("user_name") or "").lstrip("@") or user_id
        if not action_id or not channel_id:
            logger.warning("Mattermost: hermes_bridge_interact missing action/channel: %s", data)
            return
        # /model picker menus/buttons route into the picker state machine (closed-loop with the
        # gateway's on_model_selected), NOT into a user-visible TEXT turn. Drop only the hmp*
        # namespace; all other interactive callbacks keep the generic TEXT path below.
        if action_id.startswith("hmp"):
            await self._handle_model_picker_callback({
                "post_id": post_id, "action_id": action_id,
                "selected_option": selected, "context": context,
                "channel_id": channel_id, "user_id": user_id})
            return
        channel_code = await self._channel_type_code(channel_id)
        source = self.build_source(
            chat_id=channel_id,
            chat_type=_CHANNEL_TYPE_MAP.get(channel_code, "channel"),
            user_id=user_id, user_name=user_name,
            thread_id=None, message_id=post_id)
        # Both buttons and menus deliver the user's choice as plain TEXT so the
        # agent sees the picked label/value, not an invented slash command.
        label = str(context.get("label") or "").strip() if isinstance(context, dict) else ""
        question_id = str(context.get("question_id") or "").strip() if isinstance(context, dict) else ""
        text = selected or label or action_id
        logger.info("Mattermost: bridge interact action=%s selected=%r label=%r from %s in %s",
                    action_id, selected, label, user_name, channel_id)
        await self.handle_message(MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, message_id=post_id,
            raw_message={**context,
                         "action_id": action_id,
                         "selected_option": selected,
                         "response_for_question_id": question_id or None}))

    async def _handle_ws_event(self, event: Dict[str, Any]) -> None:
        evt_kind = event.get("event")
        # Custom plugin WS events arrive namespaced by the server:
        # custom_<plugin_id>_<event> (e.g. custom_hermes-bridge_hermes_bridge_command).
        if evt_kind == "reaction_added":
            if self._read_reactions:
                await self._handle_reaction_event(event.get("data", {}))
            return
        bridge_command = evt_kind and (evt_kind == "hermes_bridge_command"
                                       or evt_kind.endswith("_hermes_bridge_command"))
        bridge_interact = evt_kind and (evt_kind == "hermes_bridge_interact"
                                        or evt_kind.endswith("_hermes_bridge_interact"))
        if bridge_command:
            await self._handle_bridge_command(event.get("data", {}))
            return
        if bridge_interact:
            await self._handle_bridge_interact(event.get("data", {}))
            return
        if evt_kind != "posted":
            return
        data = event.get("data", {})
        try:
            post = json.loads(data.get("post") or "")
        except (json.JSONDecodeError, TypeError):
            return
        # Ignore own messages, system posts and redeliveries.
        sender_id, post_id = post.get("user_id", ""), post.get("id", "")
        if sender_id == self._bot_user_id or post.get("type") or self._dedup.is_duplicate(post_id):
            return
        channel_id, is_dm = post.get("channel_id", ""), data.get("channel_type", "O") == "D"
        message_text = post.get("message", "")
        # Remember the last *processed* inbound post per channel: react without an explicit
        # message_id targets this conversation's own most recent message (not the home channel).
        if channel_id and post_id:
            self._last_inbound_by_chat[channel_id] = post_id
            thread_of = post.get("root_id") or None
            if thread_of:  # remember by thread root too so react in a thread finalizes there
                self._last_inbound_by_chat[f"{channel_id}:{thread_of}"] = post_id
        if not is_dm:  # DMs need no gating; channels are mention-gated.
            message_text = self._apply_channel_gating(channel_id, message_text)
            if message_text is None:
                return
        # Thread support: replies use root_id; in thread mode a top-level channel post is itself a valid root.
        thread_id = post.get("root_id") or None
        if not thread_id and self._reply_mode == "thread" and not is_dm and post_id:
            thread_id = post_id
        if message_text[:1].isspace() and message_text.lstrip().startswith("/"):
            message_text = message_text.lstrip()
        media_urls, media_types = await self._download_attachments(post.get("file_ids") or [])
        if message_text.startswith("/"):
            msg_type = MessageType.COMMAND
        elif media_types:
            msg_type = next((mt for prefix, mt in _MEDIA_MSG_TYPES if any(m.startswith(prefix) for m in media_types)),
                            MessageType.DOCUMENT)
        else:
            msg_type = MessageType.TEXT
        source = self.build_source(
            chat_id=channel_id, chat_type=_CHANNEL_TYPE_MAP.get(data.get("channel_type", "O"), "channel"),
            user_id=sender_id, user_name=data.get("sender_name", "").lstrip("@") or sender_id,
            thread_id=thread_id, message_id=post_id)
        from gateway.platforms.base import resolve_channel_prompt
        await self.handle_message(MessageEvent(
            text=message_text, message_type=msg_type, source=source, raw_message=post, message_id=post_id,
            media_urls=media_urls or None, media_types=media_types or None,
            channel_prompt=resolve_channel_prompt(self.config.extra, channel_id, None)))


# --- Plugin standalone-send (out-of-process cron delivery via Mattermost REST) ---

async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list] = None, force_document: bool = False) -> Dict[str, Any]:
    """Send via the Mattermost v4 REST API without a live gateway adapter (out-of-process cron).

    Token/URL: ``pconfig`` with env fallback. ``media_files`` upload via ``POST /files`` and attach by
    file_id; ``thread_id`` becomes ``root_id``. ``force_document`` is signature parity only (unused).
    """
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    base_url, token = _url_and_token(pconfig)
    base_url, token = base_url.rstrip("/"), token.strip()
    if not base_url or not token:
        return {"error": "Mattermost standalone send: MATTERMOST_URL and MATTERMOST_TOKEN must both be set"}
    upload_headers = {"Authorization": f"Bearer {token}"}
    headers = {**upload_headers, "Content-Type": "application/json"}
    try:
        # One ClientSession (with proxy) covers the optional uploads + final post.
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(resolve_proxy_url(platform_env_var="MATTERMOST_PROXY"))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), **_sess_kw) as session:
            file_ids: List[str] = []
            for media in media_files or []:
                file_path = media.get("path") if isinstance(media, dict) else media
                if not file_path or not os.path.exists(file_path):
                    continue
                form = aiohttp.FormData()
                form.add_field("channel_id", chat_id)  # required so the server can attribute the upload
                with open(file_path, "rb") as fh:
                    form.add_field("files", fh.read(), filename=os.path.basename(file_path))
                async with session.post(f"{base_url}/api/v4/files", data=form, headers=upload_headers,
                                        **_req_kw) as upload_resp:
                    if upload_resp.status not in {200, 201}:
                        body = await upload_resp.text()
                        return {"error": f"Mattermost file upload failed ({upload_resp.status}): {body[:400]}"}
                    upload_data = await upload_resp.json()
                    file_ids.extend(info["id"] for info in upload_data.get("file_infos", []) if info.get("id"))
            payload: Dict[str, Any] = {"channel_id": chat_id, "message": message}
            if thread_id:
                payload["root_id"] = thread_id
            if file_ids:
                payload["file_ids"] = file_ids
            async with session.post(f"{base_url}/api/v4/posts", headers=headers, json=payload, **_req_kw) as resp:
                if resp.status not in {200, 201}:
                    body = await resp.text()
                    return {"error": f"Mattermost API error ({resp.status}): {body[:400]}"}
                data = await resp.json()
            return {"success": True, "platform": "mattermost", "chat_id": chat_id, "message_id": data.get("id")}
    except aiohttp.ClientError as exc:
        return {"error": f"Mattermost send failed (network): {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Mattermost send failed: {exc}"}


# --- Interactive setup wizard ---

def interactive_setup() -> None:
    """Guide the user through Mattermost bot setup (URL + token, allowlist, home channel)."""
    from hermes_cli.config import get_env_value, remove_env_value, save_env_value
    from hermes_cli.cli_output import prompt, prompt_yes_no, print_header, print_info, print_success

    def info(*lines: str) -> None:
        for line in lines:
            print_info(line)

    print_header("Mattermost")
    if get_env_value("MATTERMOST_TOKEN"):
        print_info("Mattermost: already configured")
        if not prompt_yes_no("Reconfigure Mattermost?", False):
            return
    info("Works with any self-hosted Mattermost instance.",
         "   1. In Mattermost: Integrations → Bot Accounts → Add Bot Account", "   2. Copy the bot token")
    print()
    mm_url = prompt("Mattermost server URL (e.g. https://mm.example.com)")
    if mm_url:
        save_env_value("MATTERMOST_URL", mm_url.rstrip("/"))
    token = prompt("Bot token", password=True)
    if not token:
        return
    save_env_value("MATTERMOST_TOKEN", token)
    print_success("Mattermost token saved")
    print()
    info("🔒 Security: Restrict who can use your bot", "   To find your user ID: click your avatar → Profile",
         "   or use the API: GET /api/v4/users/me")
    print()
    allowed_users = prompt("Allowed user IDs (comma-separated, leave empty for open access)")
    if allowed_users:
        save_env_value("MATTERMOST_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Mattermost allowlist configured")
    else:
        print_info("⚠️  No allowlist set - anyone who can message the bot can use it!")
    print()
    info("📬 Home Channel: where Hermes delivers cron job results and notifications.",
         "   To get a channel ID: click channel name → View Info → copy the ID",
         "   You can also set this later by typing /set-home in a Mattermost channel.")
    home_channel = prompt("Home channel ID (leave empty to set later with /set-home)").strip()
    if home_channel:
        save_env_value("MATTERMOST_HOME_CHANNEL", home_channel)
    elif remove_env_value("MATTERMOST_HOME_CHANNEL"):
        print_info("Home channel cleared.")
    print_info("   Open config in your editor:  hermes config edit")


# --- YAML → env config bridge (apply_yaml_config_fn) ---

_YAML_BRIDGE = (  # (yaml key, env var, yaml value → env string); allowed_channels is a whitelist
    ("require_mention", "MATTERMOST_REQUIRE_MENTION", lambda v: str(v).lower()),
    ("free_response_channels", "MATTERMOST_FREE_RESPONSE_CHANNELS", _csv),
    ("allowed_channels", "MATTERMOST_ALLOWED_CHANNELS", _csv),
    ("read_reactions", "MATTERMOST_READ_REACTIONS", lambda v: str(v).lower()),
    ("reaction_reply", "MATTERMOST_REACTION_REPLY", lambda v: str(v).lower()),
    ("bridge_command_prefix", "MATTERMOST_BRIDGE_COMMAND_PREFIX", lambda v: str(v).strip().rstrip(":")),
    ("bridge_shared_secret", "MATTERMOST_BRIDGE_SECRET", lambda v: str(v)),
    ("bridge_plugin_path", "MATTERMOST_BRIDGE_PLUGIN_PATH", lambda v: str(v)))


def _apply_yaml_config(yaml_cfg: dict, mattermost_cfg: dict) -> dict | None:
    """Translate ``config.yaml`` ``mattermost:`` keys into env vars + ``PlatformConfig.extra``.

    Env vars win over YAML (writes guarded by ``not os.getenv``). Under a multiplexed secondary
    profile the env write is skipped (it would leak into every profile via ``os.environ``); the
    values are returned so the caller seeds this profile's ``extra``, which read sites check first.

    Implements the ``apply_yaml_config_fn`` contract (#24836 / #25443). Mirrors the legacy
    ``mattermost_cfg`` block that used to live in ``gateway/config.py::load_gateway_config()`` before this
    migration.
    """
    skip_env_bridge = _profile_scoped_config_load()
    seeded: dict = {}
    for key, env, to_env in _YAML_BRIDGE:
        value = mattermost_cfg.get(key)
        if value is None and not (key == "require_mention" and key in mattermost_cfg):
            continue
        seeded[key] = value
        if not skip_env_bridge and not os.getenv(env):
            os.environ[env] = to_env(value)
    return seeded or None


def _is_connected(config) -> bool:
    """Connected when BOTH MATTERMOST_TOKEN and MATTERMOST_URL are set (``get_env_value`` looked up at
    call time so tests patching ``gateway_mod.get_env_value`` can suppress ambient env vars)."""
    import hermes_cli.gateway as gateway_mod
    return bool(
        (gateway_mod.get_env_value("MATTERMOST_TOKEN") or "").strip()
        and (gateway_mod.get_env_value("MATTERMOST_URL") or "").strip())


# --- Plugin registration entry point ---

def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="mattermost", label="Mattermost", adapter_factory=MattermostAdapter,
        check_fn=check_mattermost_requirements, validate_config=validate_mattermost_config,
        is_connected=_is_connected, required_env=["MATTERMOST_URL", "MATTERMOST_TOKEN"],
        install_hint="pip install aiohttp", setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,  # YAML→env bridge (see _YAML_BRIDGE)
        allowed_users_env="MATTERMOST_ALLOWED_USERS", allow_all_env="MATTERMOST_ALLOW_ALL_USERS",
        cron_deliver_env_var="MATTERMOST_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,  # out-of-process cron; without it `deliver=mattermost` fails
        max_message_length=MAX_POST_LENGTH, emoji="💬", allow_update_command=True)
