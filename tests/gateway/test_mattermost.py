"""Tests for Mattermost platform adapter."""
import json
import os
import time
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from gateway.run import (
    _resolve_gateway_display_bool,
    _resolve_progress_thread_id,
)


class TestMattermostProgressThreadRouting:
    def test_top_level_mattermost_progress_uses_event_message_id(self):
        assert _resolve_progress_thread_id(
            Platform.MATTERMOST,
            source_thread_id=None,
            event_message_id="top_post_123",
        ) == "top_post_123"


class TestMattermostDisplayHygiene:

    def test_mattermost_platform_opt_in_can_enable_interim_assistant_messages(self):
        """Mattermost can still opt into commentary explicitly per platform."""
        user_config = {
            "display": {
                "interim_assistant_messages": False,
                "platforms": {
                    "mattermost": {"interim_assistant_messages": True},
                },
            }
        }

        assert _resolve_gateway_display_bool(
            user_config,
            "mattermost",
            "interim_assistant_messages",
            default=True,
            platform=Platform.MATTERMOST,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True


    def test_global_thinking_progress_still_applies_to_other_platforms(self):
        """The Mattermost guard must not silently neuter Telegram/other chats."""
        user_config = {"display": {"thinking_progress": True}}

        assert _resolve_gateway_display_bool(
            user_config,
            "telegram",
            "thinking_progress",
            default=False,
            platform=Platform.TELEGRAM,
            require_platform_override_for={Platform.MATTERMOST},
        ) is True


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------

class TestMattermostConfigLoading:


    def test_mattermost_home_channel(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "mm-tok-abc123")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL", "ch_abc123")
        monkeypatch.setenv("MATTERMOST_HOME_CHANNEL_NAME", "General")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        home = config.get_home_channel(Platform.MATTERMOST)
        assert home is not None
        assert home.chat_id == "ch_abc123"
        assert home.name == "General"


# ---------------------------------------------------------------------------
# Adapter format / truncate
# ---------------------------------------------------------------------------

def _make_adapter():
    """Create a MattermostAdapter with mocked config."""
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={"url": "https://mm.example.com"},
    )
    adapter = MattermostAdapter(config)
    return adapter


class TestMattermostFormatMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_image_markdown_to_url(self):
        """![alt](url) should be converted to just the URL."""
        result = self.adapter.format_message("![cat](https://img.example.com/cat.png)")
        assert result == "https://img.example.com/cat.png"


    def test_regular_markdown_preserved(self):
        """Regular markdown (bold, italic, code) should be kept as-is."""
        content = "**bold** and *italic* and `code`"
        assert self.adapter.format_message(content) == content


class TestMattermostTruncateMessage:
    def setup_method(self):
        self.adapter = _make_adapter()


    def test_long_message_splits(self):
        msg = "a " * 2500  # 5000 chars
        chunks = self.adapter.truncate_message(msg, 4000)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= 4000


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

class TestMattermostSend:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    async def test_send_calls_api_post(self):
        """send() should POST to /api/v4/posts with channel_id and message."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post123"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)

        result = await self.adapter.send("channel_1", "Hello!")

        assert result.success is True
        assert result.message_id == "post123"

        # Verify post was called with correct URL
        call_args = self.adapter._session.post.call_args
        assert "/api/v4/posts" in call_args[0][0]
        # Verify payload
        payload = call_args[1]["json"]
        assert payload["channel_id"] == "channel_1"
        assert payload["message"] == "Hello!"


    @pytest.mark.asyncio
    async def test_send_with_thread_reply(self):
        """When reply_mode is 'thread', reply_to should become root_id."""
        self.adapter._reply_mode = "thread"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "post456"})
        mock_resp.text = AsyncMock(return_value="")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        # send() now calls _resolve_root_id → _api_get("posts/<id>") first
        # to make sure root_id points to a thread root, so we need to mock
        # the GET too.  Return an empty dict (no root_id) so the resolver
        # falls back to the original reply_to as the root.
        mock_get_resp = AsyncMock()
        mock_get_resp.status = 200
        mock_get_resp.json = AsyncMock(return_value={"id": "root_post", "root_id": ""})
        mock_get_resp.text = AsyncMock(return_value="")
        mock_get_resp.__aenter__ = AsyncMock(return_value=mock_get_resp)
        mock_get_resp.__aexit__ = AsyncMock(return_value=False)

        self.adapter._session.post = MagicMock(return_value=mock_resp)
        self.adapter._session.get = MagicMock(return_value=mock_get_resp)

        result = await self.adapter.send("channel_1", "Reply!", reply_to="root_post")

        assert result.success is True
        payload = self.adapter._session.post.call_args[1]["json"]
        assert payload["root_id"] == "root_post"


    @pytest.mark.asyncio
    async def test_progress_send_with_invalid_thread_root_never_falls_back_flat(self):
        """Tool/status/progress bubbles must stay quiet when the thread is broken."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"

    @pytest.mark.asyncio
    async def test_notify_send_with_invalid_thread_root_falls_back_flat_with_warning(self):
        """Notify-worthy replies may fall back flat so the answer is not lost."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._last_post_status = 400
        self.adapter._last_post_error = "api.context.invalid_param.app_error: invalid root_id"
        self.adapter._api_post = AsyncMock(side_effect=[{}, {"id": "flat_final"}])

        result = await self.adapter.send(
            "channel_1",
            "Final answer body",
            reply_to="bad_root",
            metadata={"notify": True},
        )

        assert result.success is True
        assert result.message_id == "flat_final"
        assert self.adapter._api_post.call_count == 2
        threaded_payload = self.adapter._api_post.call_args_list[0][0][1]
        flat_payload = self.adapter._api_post.call_args_list[1][0][1]
        assert threaded_payload["root_id"] == "bad_root"
        assert "root_id" not in flat_payload
        assert flat_payload["channel_id"] == "channel_1"
        assert "Mattermost thread delivery failed" in flat_payload["message"]
        assert "Final answer body" in flat_payload["message"]


    @pytest.mark.asyncio
    async def test_progress_send_with_broken_thread_and_no_recorded_error_stays_quiet(self):
        """Same rule when no post error was recorded: still no flat fallback."""
        self.adapter._reply_mode = "thread"
        self.adapter._api_get = AsyncMock(return_value={"id": "bad_root", "root_id": ""})
        self.adapter._api_post = AsyncMock(return_value={})

        result = await self.adapter.send(
            "channel_1",
            "⚙️ terminal...",
            metadata={"thread_id": "bad_root"},
        )

        assert result.success is False
        assert self.adapter._api_post.call_count == 1
        payload = self.adapter._api_post.call_args_list[0][0][1]
        assert payload["root_id"] == "bad_root"


# ---------------------------------------------------------------------------
# WebSocket event parsing
# ---------------------------------------------------------------------------

class TestMattermostWebSocketParsing:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        # Mock handle_message to capture the MessageEvent without processing
        self.adapter.handle_message = AsyncMock()

    @pytest.mark.asyncio
    async def test_parse_posted_event(self):
        """'posted' events should extract message from double-encoded post JSON."""
        post_data = {
            "id": "post_abc",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id Hello from Matrix!",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),  # double-encoded JSON string
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        # @mention is stripped from the message text
        assert msg_event.text == "Hello from Matrix!"
        assert msg_event.message_id == "post_abc"


    @pytest.mark.asyncio
    async def test_ignore_system_posts(self):
        """Posts with a 'type' field (system messages) should be ignored."""
        post_data = {
            "id": "sys_post",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "user joined",
            "type": "system_join_channel",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert not self.adapter.handle_message.called


    @pytest.mark.asyncio
    async def test_leading_space_slash_command_is_command(self):
        """Mattermost mobile suggests leading-space slash commands."""
        post_data = {
            "id": "post_cmd",
            "user_id": "user_123",
            "channel_id": "chan_dm",
            "message": " /new",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "D",
                "sender_name": "@bob",
            },
        }

        await self.adapter._handle_ws_event(event)
        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "/new"
        assert msg_event.message_type is MessageType.COMMAND
        assert msg_event.get_command() == "new"


# ---------------------------------------------------------------------------
# Mention behavior (require_mention + free_response_channels)
# ---------------------------------------------------------------------------

class TestMattermostMentionBehavior:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter._bot_username = "hermes-bot"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, message, channel_type="O", channel_id="chan_456"):
        post_data = {
            "id": "post_mention",
            "user_id": "user_123",
            "channel_id": channel_id,
            "message": message,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": channel_type,
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_require_mention_true_skips_without_mention(self):
        """Default: messages without @mention in channels are skipped."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            os.environ.pop("MATTERMOST_FREE_RESPONSE_CHANNELS", None)
            await self.adapter._handle_ws_event(self._make_event("hello"))
            assert not self.adapter.handle_message.called


    @pytest.mark.asyncio
    async def test_free_response_channel_responds_without_mention(self):
        """Messages in free-response channels don't need @mention."""
        with patch.dict(os.environ, {"MATTERMOST_FREE_RESPONSE_CHANNELS": "chan_456,chan_789"}):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            await self.adapter._handle_ws_event(self._make_event("hello", channel_id="chan_456"))
            assert self.adapter.handle_message.called


# ---------------------------------------------------------------------------
# File upload (send_image)
# ---------------------------------------------------------------------------

class TestMattermostFileUpload:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._session = MagicMock()

    @pytest.mark.asyncio
    @patch("tools.url_safety.is_safe_url", return_value=True)
    async def test_send_image_downloads_and_uploads(self, _mock_safe):
        """send_image should download the URL, upload via /api/v4/files, then post."""
        # Mock the download (GET)
        mock_dl_resp = AsyncMock()
        mock_dl_resp.status = 200
        mock_dl_resp.read = AsyncMock(return_value=b"\x89PNG\x00fake-image-data")
        mock_dl_resp.content_type = "image/png"
        mock_dl_resp.__aenter__ = AsyncMock(return_value=mock_dl_resp)
        mock_dl_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the upload (POST to /files)
        mock_upload_resp = AsyncMock()
        mock_upload_resp.status = 200
        mock_upload_resp.json = AsyncMock(return_value={
            "file_infos": [{"id": "file_abc123"}]
        })
        mock_upload_resp.text = AsyncMock(return_value="")
        mock_upload_resp.__aenter__ = AsyncMock(return_value=mock_upload_resp)
        mock_upload_resp.__aexit__ = AsyncMock(return_value=False)

        # Mock the post (POST to /posts)
        mock_post_resp = AsyncMock()
        mock_post_resp.status = 200
        mock_post_resp.json = AsyncMock(return_value={"id": "post_with_file"})
        mock_post_resp.text = AsyncMock(return_value="")
        mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
        mock_post_resp.__aexit__ = AsyncMock(return_value=False)

        # Route calls: first GET (download), then POST (upload), then POST (create post)
        self.adapter._session.get = MagicMock(return_value=mock_dl_resp)
        post_call_count = 0
        original_post_returns = [mock_upload_resp, mock_post_resp]

        def post_side_effect(*args, **kwargs):
            nonlocal post_call_count
            resp = original_post_returns[min(post_call_count, len(original_post_returns) - 1)]
            post_call_count += 1
            return resp

        self.adapter._session.post = MagicMock(side_effect=post_side_effect)

        result = await self.adapter.send_image(
            "channel_1", "https://img.example.com/cat.png", caption="A cat"
        )

        assert result.success is True
        assert result.message_id == "post_with_file"


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------

class TestMattermostDedup:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        # Mock handle_message to capture calls without processing
        self.adapter.handle_message = AsyncMock()


    def test_prune_seen_clears_expired(self):
        """Dedup cache should remove entries older than TTL on overflow."""
        now = time.time()
        dedup = self.adapter._dedup
        # Fill with enough expired entries to trigger pruning
        for i in range(dedup._max_size + 10):
            dedup._seen[f"old_{i}"] = now - 600  # 10 min ago (older than default TTL)

        # Add a fresh one
        dedup._seen["fresh"] = now

        # Trigger pruning by calling is_duplicate with a new entry (over max_size)
        dedup.is_duplicate("trigger_prune")

        # Old entries should be pruned, fresh one kept
        assert "fresh" in dedup._seen
        assert len(dedup._seen) < dedup._max_size + 10


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

class TestMattermostRequirements:
    def test_check_requirements_with_token_and_url(self, monkeypatch):
        monkeypatch.setenv("MATTERMOST_TOKEN", "test-token")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")
        from plugins.platforms.mattermost.adapter import check_mattermost_requirements
        assert check_mattermost_requirements() is True


    def test_validate_config_accepts_platform_values(self, monkeypatch):
        monkeypatch.delenv("MATTERMOST_TOKEN", raising=False)
        monkeypatch.delenv("MATTERMOST_URL", raising=False)
        from plugins.platforms.mattermost.adapter import validate_mattermost_config

        config = PlatformConfig(
            enabled=True,
            token="cfg-token",
            extra={"url": "https://mm.example.com"},
        )
        assert validate_mattermost_config(config) is True


# ---------------------------------------------------------------------------
# Media type propagation (MIME types, not bare strings)
# ---------------------------------------------------------------------------

class TestMattermostMediaTypes:
    """Verify that media_types contains actual MIME types (e.g. 'image/png')
    rather than bare category strings ('image'), so downstream
    ``mtype.startswith("image/")`` checks in run.py work correctly."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._bot_user_id = "bot_user_id"
        self.adapter.handle_message = AsyncMock()

    def _make_event(self, file_ids):
        post_data = {
            "id": "post_media",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "@bot_user_id file attached",
            "file_ids": file_ids,
        }
        return {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

    @pytest.mark.asyncio
    async def test_image_media_type_is_full_mime(self):
        """An image attachment should produce 'image/png', not 'image'."""
        file_info = {"name": "photo.png", "mime_type": "image/png"}
        self.adapter._api_get = AsyncMock(return_value=file_info)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"\x89PNG fake")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        self.adapter._session = MagicMock()
        self.adapter._session.get = MagicMock(return_value=mock_resp)

        with patch("gateway.platforms.base.cache_image_from_bytes", return_value="/tmp/photo.png"):
            await self.adapter._handle_ws_event(self._make_event(["file1"]))

        msg = self.adapter.handle_message.call_args[0][0]
        assert msg.media_types == ["image/png"]
        assert msg.media_types[0].startswith("image/")


@pytest.mark.asyncio
async def test_mattermost_top_level_channel_post_is_thread_root():
    adapter = _make_adapter()
    adapter._reply_mode = "thread"
    adapter._bot_user_id = "bot_user_id"
    adapter._bot_username = "hermes-bot"
    adapter.handle_message = AsyncMock()
    post_data = {
        "id": "top_post_123",
        "user_id": "user_123",
        "channel_id": "chan_456",
        "message": "@hermes-bot start work",
        "root_id": "",
    }
    event = {
        "event": "posted",
        "data": {
            "post": json.dumps(post_data),
            "channel_type": "O",
            "sender_name": "@alice",
        },
    }

    await adapter._handle_ws_event(event)

    msg_event = adapter.handle_message.call_args[0][0]
    assert msg_event.source.thread_id == "top_post_123"
    assert msg_event.source.message_id == "top_post_123"
    assert msg_event.message_id == "top_post_123"


# ---------------------------------------------------------------------------
# Multiplex secondary-profile scope
# ---------------------------------------------------------------------------
#
# __init__'s url/reply_mode, validate_mattermost_config's url,
# _standalone_send's url, and _handle_ws_event's require_mention/
# free_response_channels/allowed_channels, all previously read raw
# os.getenv unconditionally (only MATTERMOST_TOKEN was already scoped).
# _apply_yaml_config also wrote MATTERMOST_REQUIRE_MENTION/
# MATTERMOST_FREE_RESPONSE_CHANNELS/MATTERMOST_ALLOWED_CHANNELS into the
# process-global os.environ unconditionally. Under multiplex, os.environ
# holds the DEFAULT profile's YAML-to-env bridge output -- a secondary
# profile with its own (different or absent) Mattermost config would
# silently connect to the default profile's server, or have its
# mention-gating/channel-allowlist decisions driven by the default
# profile's settings. Mirrors the LINE/DingTalk/IRC fix for #98738.

@pytest.fixture
def multiplex_scope():
    """Install multiplex + a secondary-profile secret scope; restore after."""
    tokens = []

    def install(scope=None):
        from agent.secret_scope import set_multiplex_active, set_secret_scope

        set_multiplex_active(True)
        tokens.append(set_secret_scope(scope or {}))
        return tokens[-1]

    yield install

    from agent.secret_scope import reset_secret_scope, set_multiplex_active

    for token in reversed(tokens):
        reset_secret_scope(token)
    set_multiplex_active(False)


@pytest.fixture
def default_profile_env(monkeypatch):
    """The default profile's YAML-to-env bridge output in os.environ."""
    monkeypatch.setenv("MATTERMOST_URL", "https://default.example.com")
    monkeypatch.setenv("MATTERMOST_REPLY_MODE", "thread")
    monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "false")
    monkeypatch.setenv("MATTERMOST_FREE_RESPONSE_CHANNELS", "chan_default")
    monkeypatch.setenv("MATTERMOST_ALLOWED_CHANNELS", "chan_default")


class TestMultiplexProfileScope:

    @pytest.mark.asyncio
    async def test_ws_event_gating_uses_scoped_settings_not_default(
        self, monkeypatch
    ):
        """A secondary profile's own require_mention/free_response_channels/
        allowed_channels (installed via the scope) must gate its messages --
        not the default profile's bridged settings."""
        from agent.secret_scope import (
            reset_secret_scope,
            set_multiplex_active,
            set_secret_scope,
        )
        from plugins.platforms.mattermost.adapter import MattermostAdapter

        monkeypatch.setenv("MATTERMOST_REQUIRE_MENTION", "true")
        monkeypatch.delenv("MATTERMOST_FREE_RESPONSE_CHANNELS", raising=False)

        adapter = _make_adapter()
        adapter._bot_user_id = "bot_user_id"
        adapter._bot_username = "hermes-bot"
        adapter.handle_message = AsyncMock()

        post_data = {
            "id": "post_scoped",
            "user_id": "user_123",
            "channel_id": "chan_456",
            "message": "hello with no mention",
        }
        event = {
            "event": "posted",
            "data": {
                "post": json.dumps(post_data),
                "channel_type": "O",
                "sender_name": "@alice",
            },
        }

        set_multiplex_active(True)
        token = set_secret_scope({"MATTERMOST_REQUIRE_MENTION": "false"})
        try:
            await adapter._handle_ws_event(event)
        finally:
            reset_secret_scope(token)
            set_multiplex_active(False)

        # The profile's own scope disables require_mention -- the message
        # must be dispatched even without an @mention, despite the default
        # profile's env bridge saying require_mention=true.
        assert adapter.handle_message.called

    def test_apply_yaml_config_scoped_skips_env_write_and_seeds_extra(
        self, multiplex_scope
    ):
        from plugins.platforms.mattermost.adapter import _apply_yaml_config

        multiplex_scope()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATTERMOST_REQUIRE_MENTION", None)
            seeded = _apply_yaml_config({}, {"require_mention": False, "allowed_channels": ["c1"]})
            assert seeded == {"require_mention": False, "allowed_channels": ["c1"]}
            # Under a secondary profile's scope the env bridge must be
            # skipped -- writing here would leak into every other profile's
            # os.environ.
            assert "MATTERMOST_REQUIRE_MENTION" not in os.environ



# ---------------------------------------------------------------------------
# Reaction reading (read_reactions): WS reaction_added → passive sidecar note
# ---------------------------------------------------------------------------

class TestMattermostReadReactions:
    def _rx_adapter(self, read=True):
        from plugins.platforms.mattermost.adapter import MattermostAdapter
        config = PlatformConfig(
            enabled=True,
            token="test-token",
            extra={"url": "https://mm.example.com", "read_reactions": str(read).lower()},
        )
        a = MattermostAdapter(config)
        a._bot_user_id = "bot_id"
        a._api_get = AsyncMock()
        a._channel_type_code = AsyncMock(return_value="O")
        # fake gateway runner with the sidecar-staging hook
        a.gateway_runner = MagicMock()
        a.gateway_runner._set_pending_turn_sidecar_notes = MagicMock()
        a.gateway_runner._session_key_for_source = MagicMock(return_value="agent:main:mattermost:thread:chan_9:root_0")
        return a

    def _rx_evt(self, **over):
        payload = {"user_id": "alice", "post_id": "post_1", "emoji_name": "+1", "channel_id": "chan_9"}
        payload.update(over)
        return {"event": "reaction_added", "data": {"reaction": json.dumps(payload)}}

    @pytest.mark.asyncio
    async def test_reaction_reply_mode_forks_full_turn(self):
        """reaction_reply=true surfaces the reaction as an active internal turn (may reply)."""
        a = self._rx_adapter(read=True)
        a._reaction_reply = True
        a.handle_message = AsyncMock()
        a._api_get.return_value = {"id": "post_1", "user_id": "bot_id", "channel_id": "chan_9"}
        await a._handle_ws_event(self._rx_evt())
        a.handle_message.assert_awaited_once()
        msg = a.handle_message.await_args.args[0]
        assert msg.internal is True and "[Reaction]" in msg.text
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_resolves_dm_channel_type_for_session(self):
        """reaction has no channel_type in WS; the adapter must resolve D via the API so the
        note is staged under the DM session key, not a channel key."""
        a = self._rx_adapter(read=True)
        a._channel_type_code = AsyncMock(return_value="D")
        a._api_get.return_value = {"id": "post_1", "user_id": "bot_id", "channel_id": "chan_9"}
        await a._handle_ws_event(self._rx_evt())
        a._channel_type_code.assert_awaited_once_with("chan_9")
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_called_once()

    @pytest.mark.asyncio
    async def test_reaction_stages_passive_sidecar_note(self):
        a = self._rx_adapter(read=True)
        a._api_get.return_value = {"id": "post_1", "user_id": "bot_id", "channel_id": "chan_9"}
        await a._handle_ws_event(self._rx_evt())
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_called_once()
        sk, notes = a.gateway_runner._set_pending_turn_sidecar_notes.call_args.args
        assert len(notes) == 1 and "[Reaction]" in notes[0] and "+1" in notes[0]
        assert isinstance(sk, str) and sk

    @pytest.mark.asyncio
    async def test_reaction_no_handle_message_turn(self):
        """A reaction must NOT spawn a full agent turn (no visible reply)."""
        a = self._rx_adapter(read=True)
        a._api_get.return_value = {"id": "post_1", "user_id": "bot_id", "channel_id": "chan_9"}
        a.handle_message = AsyncMock()
        await a._handle_ws_event(self._rx_evt())
        a.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_ignored_when_off(self):
        a = self._rx_adapter(read=False)
        await a._handle_ws_event(self._rx_evt())
        a._api_get.assert_not_awaited()
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_not_called()

    @pytest.mark.asyncio
    async def test_own_loop_dropped(self):
        a = self._rx_adapter(read=True)
        await a._handle_ws_event(self._rx_evt(user_id="bot_id"))
        a._api_get.assert_not_awaited()
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_on_non_owned_post_ignored(self):
        a = self._rx_adapter(read=True)
        a._api_get.return_value = {"id": "post_1", "user_id": "other", "channel_id": "chan_9"}
        await a._handle_ws_event(self._rx_evt())
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaction_in_bot_thread_accepted(self):
        a = self._rx_adapter(read=True)
        a._api_get.side_effect = [
            {"id": "reply_1", "user_id": "alice", "root_id": "root_0", "channel_id": "chan_9"},
            {"id": "root_0", "user_id": "bot_id", "channel_id": "chan_9"},
        ]
        await a._handle_ws_event(self._rx_evt(post_id="reply_1", emoji_name="❤️"))
        a.gateway_runner._set_pending_turn_sidecar_notes.assert_called_once()
        notes = a.gateway_runner._set_pending_turn_sidecar_notes.call_args.args[1]
        assert "❤️" in notes[0] and "твоём треде" in notes[0]

    @pytest.mark.asyncio
    async def test_staged_note_is_routed_to_thread_session(self):
        a = self._rx_adapter(read=True)
        a._api_get.side_effect = [
            {"id": "reply_1", "user_id": "alice", "root_id": "root_0", "channel_id": "chan_9"},
            {"id": "root_0", "user_id": "bot_id", "channel_id": "chan_9"},
        ]
        await a._handle_ws_event(self._rx_evt(post_id="reply_1"))
        sk = a.gateway_runner._set_pending_turn_sidecar_notes.call_args.args[0]
        assert "root_0" in sk


# ---------------------------------------------------------------------------
# Sending reactions (add_reaction / remove_reaction)
# ---------------------------------------------------------------------------

class TestMattermostSendReaction:
    @pytest.mark.asyncio
    async def test_add_reaction_posts_to_reactions_endpoint(self):
        a = _make_adapter()
        a._bot_user_id = "bot_id"
        a._api_post = AsyncMock(return_value={"user_id": "bot_id", "post_id": "post_1", "emoji_name": "+1"})
        res = await a.add_reaction(chat_id="chan_9", message_id="post_1", emoji="👍")
        a._api_post.assert_awaited_once()
        path = a._api_post.await_args.args[0]
        payload = a._api_post.await_args.args[1]
        assert path == "reactions"
        # "👍" (glyph) must be normalized to the Mattermost name "+1".
        assert payload == {"user_id": "bot_id", "post_id": "post_1", "emoji_name": "+1"}
        assert res["success"] is True

    def test_normalize_reaction_emoji_glyph_to_name(self):
        from plugins.platforms.mattermost.adapter import _normalize_reaction_emoji as norm
        assert norm("👍") == "+1"
        assert norm("❤️") == "heart"
        assert norm("✅") == "white_check_mark"
        assert norm("➕") == "heavy_plus_sign"
        assert norm("thumbsup") == "thumbsup"          # already a name: passthrough
        assert norm("+1") == "+1"
        assert norm(":heart:") == "heart"               # colon wrapper stripped
        assert norm("") == ""

    @pytest.mark.asyncio
    async def test_add_reaction_requires_message_id_no_fallback(self):
        """No fallback: add_reaction without message_id must fail explicitly (never guess/last-post)."""
        a = _make_adapter()
        a._bot_user_id = "bot_id"
        a._api_get = AsyncMock()
        a._api_post = AsyncMock()
        res = await a.add_reaction(chat_id="chan_9", message_id="", emoji="👍")
        assert res["success"] is False
        a._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_add_reaction_reports_failure_verbatim(self):
        """An API rejection must be surfaced as success:false, never a silent ''{}''."""
        a = _make_adapter()
        a._bot_user_id = "bot_id"
        a._api_post = AsyncMock(return_value={})  # _api returns {} on >=400
        res = await a.add_reaction(chat_id="chan_9", message_id="post_1", emoji="no_such_emoji")
        assert res["success"] is False
        assert "error" in res

    @pytest.mark.asyncio
    async def test_remove_reaction_calls_delete_with_emoji(self):
        a = _make_adapter()
        a._bot_user_id = "bot_id"
        a._api = AsyncMock(return_value={})
        res = await a.remove_reaction(chat_id="chan_9", message_id="post_1", emoji="👍")
        method, path = a._api.await_args.args[0], a._api.await_args.args[1]
        assert method == "DELETE"
        assert path == "users/bot_id/posts/post_1/reactions/+1"
        assert res["success"] is True


# ---------------------------------------------------------------------------
# Triggering post id is surfaced to the model (react_message needs it)
# ---------------------------------------------------------------------------

class TestMattermostTriggeringPostId:

    def test_prepend_inbound_context_exposes_post_id(self):
        from gateway.run_inbound import GatewayInboundMixin
        from gateway.platforms.base import MessageEvent, MessageType, SessionSource
        from gateway.config import Platform
        source = SessionSource(
            platform=Platform.MATTERMOST, chat_id="chan_9", chat_type="dm", message_id="post_abc",
        )
        # build a real MessageEvent
        event = MessageEvent(text="поставь реакцию", message_type=MessageType.TEXT,
                             source=source, message_id="post_abc")
        out = GatewayInboundMixin._prepend_inbound_reply_context(event, source, "поставь реакцию")
        assert "post_abc" in out
        assert "react_message" in out

    def test_prepend_inbound_other_platform_unchanged_for_missing_id(self):
        from gateway.run_inbound import GatewayInboundMixin
        from gateway.platforms.base import MessageEvent, MessageType, SessionSource
        from gateway.config import Platform
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm")
        event = MessageEvent(text="hi", message_type=MessageType.TEXT, source=source, message_id=None)
        assert GatewayInboundMixin._prepend_inbound_reply_context(event, source, "hi") == "hi"


# ---------------------------------------------------------------------------
# Native hermes-bridge WS events: slash commands & interactive actions
# (plugins/platforms/mattermost/bridge) relayed into MessageEvent.COMMAND
# ---------------------------------------------------------------------------

class TestMattermostBridgeEvents:

    def _bridge_adapter(self, **extra):
        from plugins.platforms.mattermost.adapter import MattermostAdapter
        config = PlatformConfig(
            enabled=True, token="test-token",
            extra={"url": "https://mm.example.com", **extra},
        )
        a = MattermostAdapter(config)
        a._bot_user_id = "bot_id"
        a._channel_type_code = AsyncMock(return_value="O")
        a.handle_message = AsyncMock()
        return a

    def _cmd_evt(self, **over):
        data = {
            "trigger": "new", "args": "brief", "channel_id": "chan_9",
            "channel_type": "O", "user_id": "alice", "user_name": "@alice",
            "thread_id": "", "response_url": "",
        }
        data.update(over)
        return {"event": "hermes_bridge_command", "data": data}

    @pytest.mark.asyncio
    async def test_bridge_command_builds_command_event(self):
        a = self._bridge_adapter()
        await a._handle_ws_event(self._cmd_evt())
        a.handle_message.assert_awaited_once()
        msg = a.handle_message.await_args.args[0]
        assert msg.message_type == MessageType.COMMAND
        assert msg.text == "/new brief"
        assert msg.source.chat_id == "chan_9"
        assert msg.source.chat_type == "channel"
        assert msg.source.user_id == "alice"

    @pytest.mark.asyncio
    async def test_bridge_command_without_args(self):
        a = self._bridge_adapter()
        await a._handle_ws_event(self._cmd_evt(args="", trigger="stop"))
        msg = a.handle_message.await_args.args[0]
        assert msg.text == "/stop"

    @pytest.mark.asyncio
    async def test_bridge_command_missing_channel_dropped(self):
        a = self._bridge_adapter()
        await a._handle_ws_event(self._cmd_evt(channel_id=""))
        a.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_command_dm_resolves_channel_type(self):
        """No channel_type in payload → adapter resolves via API (D = dm session key)."""
        a = self._bridge_adapter()
        a._channel_type_code = AsyncMock(return_value="D")
        a._api_get = AsyncMock(return_value={"type": "D"})
        await a._handle_ws_event(self._cmd_evt(channel_type=""))
        msg = a.handle_message.await_args.args[0]
        assert msg.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_bridge_interact_builds_command_event(self):
        a = self._bridge_adapter()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "approve", "label": "Approve", "post_id": "post_1",
            "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "approve", "label": "Approve"},
        }}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()
        msg = a.handle_message.await_args.args[0]
        assert msg.message_type == MessageType.TEXT
        # Button click surfaces the label as text — never an invented slash command.
        assert msg.text == "Approve"
        assert msg.source.user_id == "bob"

    @pytest.mark.asyncio
    async def test_bridge_interact_menu_selection_is_text(self):
        """A menu selection carries the picked value as the agent-facing text."""
        a = self._bridge_adapter()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "pick", "selected_option": "chocolate",
            "post_id": "post_1", "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "pick", "selected_option": "chocolate", "question_id": "q_1234"},
        }}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()
        msg = a.handle_message.await_args.args[0]
        assert msg.message_type == MessageType.TEXT
        assert msg.text == "chocolate"
        assert msg.raw_message.get("action_id") == "pick"
        assert msg.raw_message.get("selected_option") == "chocolate"
        assert msg.raw_message.get("response_for_question_id") == "q_1234"

    @pytest.mark.asyncio
    async def test_bridge_command_namespaced_prefix_still_hits(self):
        """Server namespaces plugin events as custom_<plugin_id>_<event>."""
        a = self._bridge_adapter()
        await a._handle_ws_event({"event": "custom_hermes-bridge_hermes_bridge_command", "data": self._cmd_evt()["data"]})
        a.handle_message.assert_awaited_once()
        msg = a.handle_message.await_args.args[0]
        assert msg.text == "/new brief"
        assert msg.message_type == MessageType.COMMAND

    @pytest.mark.asyncio
    async def test_bridge_command_strips_namespace_prefix(self):
        """trigger 'hermes:new' (namespace prefix) must become canonical '/new'."""
        a = self._bridge_adapter(bridge_command_prefix="hermes")
        evt = {"event": "hermes_bridge_command", "data": self._cmd_evt(trigger="hermes:new")["data"]}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()
        assert a.handle_message.await_args.args[0].text == "/new brief"

    @pytest.mark.asyncio
    async def test_bridge_command_no_prefix_keeps_trigger(self):
        a = self._bridge_adapter(bridge_command_prefix="")
        evt = {"event": "hermes_bridge_command", "data": self._cmd_evt(trigger="customthing")["data"]}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()
        assert a.handle_message.await_args.args[0].text == "/customthing brief"

    @pytest.mark.asyncio
    async def test_unknown_ws_event_still_ignored(self):
        a = self._bridge_adapter()
        await a._handle_ws_event({"event": "whatever_new", "data": {}})
        a.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_control_command_bypasses_active_session_guard(self):
        """A control command (/stop, /new) arriving over the bridge while the
        session is busy MUST be dispatched inline, never queued as pending —
        otherwise it leaks as user text or deadlocks (gateway invariant, #4926)."""
        from gateway.config import Platform
        from gateway.session import SessionSource
        from gateway.platforms.base import MessageEvent, MessageType

        a = self._bridge_adapter(bridge_command_prefix="hermes")
        # Restore the REAL handle_message (the helper installs an AsyncMock stub,
        # which bypasses the active-session guard logic we're testing).
        from gateway.platforms.base import BasePlatformAdapter
        a.handle_message = BasePlatformAdapter.handle_message.__get__(a, type(a))
        # Real handler that records how the command reached the runner.
        dispatched = []
        async def _handler(event):
            dispatched.append((event.get_command(), event.text))
            return ""
        a._message_handler = _handler
        a._busy_session_handler = None
        a._busy_text_mode = ""
        a._pending_messages = {}

        # Establish the session key (no message_id — matches the bridge event),
        # then simulate the agent is busy on it.
        src = SessionSource(platform=Platform.MATTERMOST, chat_id="chan_9",
                            chat_type="dm", user_id="alice")
        busy = MessageEvent(text="placeholder", message_type=MessageType.TEXT, source=src)
        key = a._event_session_key(busy)
        a._active_sessions = {key: asyncio.Event()}

        await a._handle_ws_event({"event": "hermes_bridge_command",
                          "data": self._cmd_evt(trigger="hermes:stop", channel_type="D", args="")["data"]})

        # Must have been dispatched inline (bypass), NOT queued.
        assert a._pending_messages == {}
        assert dispatched == [("stop", "/stop")]


# ---------------------------------------------------------------------------
# Registry sync: Hermes commands -> native plugin (with namespace prefix)
# ---------------------------------------------------------------------------

class TestMattermostBridgeRegistrySync:

    def test_gateway_specs_namespaced_and_no_cli_only(self):
        from plugins.platforms.mattermost.bridge_registry import _gateway_command_specs
        from hermes_cli.commands import COMMAND_REGISTRY
        specs = _gateway_command_specs("hermes")
        triggers = {s["trigger"] for s in specs}
        cli_only = {c.name for c in COMMAND_REGISTRY if c.cli_only and not c.gateway_config_gate}
        assert triggers  # non-empty
        assert all(t.startswith("hermes:") for t in triggers)
        # no cli_only command leaked into the registry
        leaked = {t.split(":", 1)[1] for t in triggers} & cli_only
        assert not leaked

    def test_push_command_registry_sends_ok(self):
        """push_command_registry POSTs to the plugin and returns its response."""
        from plugins.platforms.mattermost import bridge_registry
        import aiohttp
        import asyncio

        captured = {}

        class FakeResp:
            def __init__(self, status=200, body=None):
                self.status = status
                self._body = body or {"ok": True, "registered": 3}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def json(self, **kw):
                return self._body

        class FakeSession:
            def __init__(self):
                self.closed = False
            async def post(self, url, **kw):
                captured["url"] = url
                captured["headers"] = kw.get("headers")
                captured["payload"] = kw.get("json")
                return FakeResp()

        async def go():
            sess = FakeSession()
            res = await bridge_registry.push_command_registry(
                base_url="https://mm.example.com", shared_secret="sekret",
                prefix="hermes", session=sess)
            return res

        res = asyncio.run(go())
        assert res["ok"] is True
        assert captured["url"].endswith("/plugins/hermes-bridge/config")
        assert captured["headers"]["Authorization"] == "Bearer sekret"
        cmds = captured["payload"]["commands"]
        assert all(c["trigger"].startswith("hermes:") for c in cmds)
        assert captured["payload"]["replace"] is True

    def test_push_command_registry_survives_http_error(self):
        """A non-2xx plugin response must NOT raise (fail-open, startup-safe)."""
        from plugins.platforms.mattermost import bridge_registry
        import asyncio

        class FakeResp:
            status = 401
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def json(self, **kw):
                return {"id": "app.error", "message": "nope"}

        class FakeSession:
            closed = False
            async def post(self, url, **kw):
                return FakeResp()

        res = asyncio.run(bridge_registry.push_command_registry(
            base_url="https://mm.example.com", shared_secret="x", prefix="hermes",
            session=FakeSession()))
        assert res["ok"] is False


# ---------------------------------------------------------------------------
# Interactive messages (buttons/menus) + ephemeral replies
# ---------------------------------------------------------------------------

class TestMattermostInteractiveSend:

    def _adapter(self):
        a = _make_adapter()
        a._session = MagicMock()
        return a

    @pytest.mark.asyncio
    async def test_send_interactive_builds_actions_payload(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "post_int_1"})
        res = await a.send_interactive(
            "chan_9", "Pick one", buttons=[{"id": "yes", "label": "Yes", "style": "primary"}],
            menu={"id": "pick", "placeholder": "Choose", "options": [{"label": "A", "value": "a"}]})
        assert res.success is True
        path, payload = a._api_post.call_args.args
        assert path == "posts"
        # Text must appear only in the attachment card, never duplicated as the
        # post message too (else the question renders twice).
        assert payload["message"] == ""
        attach = payload["props"]["attachments"][0]
        assert attach["text"] == "Pick one"
        actions = attach["actions"]
        # Multi-control post (button + menu) → auto-append a Submit button.
        assert len(actions) == 3
        btn = actions[0]
        assert btn["type"] == "button" and btn["id"] == "yes"
        assert btn["integration"]["url"] == "/plugins/hermes-bridge/interact"
        assert btn["integration"]["context"]["action_id"] == "yes"
        assert btn["integration"]["context"]["label"] == "Yes"
        assert "question_id" not in btn["integration"]["context"]
        menu_action = actions[1]
        assert menu_action["type"] == "select"
        assert menu_action["options"] == [{"text": "A", "value": "a"}]
        # Auto-added Submit button: submit:true in context so the plugin relays the
        # whole form on its click.
        submit = actions[2]
        assert submit["type"] == "button" and submit["name"] == "Готово"
        assert submit["integration"]["context"]["submit"] is True
        assert submit["integration"]["context"]["action_id"] == "submit_form"

    @pytest.mark.asyncio
    async def test_send_interactive_single_button_no_auto_submit(self):
        """A single-button post is an immediate choice — no Submit appended."""
        a = self._adapter()
        a._session = MagicMock()
        a._api_post = AsyncMock(return_value={"id": "p1"})
        a._api = AsyncMock(return_value={})
        res = await a.send_interactive(
            "chan_9", "Да или нет", buttons=[{"id": "yes", "label": "Да"}])
        assert res.success is True
        actions = a._api_post.call_args.args[1]["props"]["attachments"][0]["actions"]
        assert len(actions) == 1
        assert actions[0]["id"] == "yes"
        assert actions[0]["integration"]["context"].get("submit") is None

    @pytest.mark.asyncio
    async def test_send_interactive_requires_action(self):
        a = self._adapter()
        res = await a.send_interactive("chan_9", "hi")
        assert res.success is False

    @pytest.mark.asyncio
    async def test_send_ephemeral_posts_to_ephemeral_endpoint(self):
        a = self._adapter()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value="")
        a._session.post = MagicMock(return_value=mock_resp)
        a._session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        a._session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        res = await a.send_ephemeral("chan_9", "bob", "psst")
        assert res.success is True
        url, kwargs = a._session.post.call_args.args[0], a._session.post.call_args.kwargs
        assert url.endswith("/api/v4/posts/ephemeral")
        body = kwargs["json"]
        assert body["user_id"] == "bob"
        assert body["post"]["channel_id"] == "chan_9"
        assert body["post"]["message"] == "psst"


def test_send_interactive_tool_inherits_session_thread():
    """When the bot replies inside a thread, send_interactive_message inherits
    the current thread_id from the session (buttons don't land in the channel root)."""
    from tools import mattermost_interactive_tools as mit
    import gateway.session_context as sc
    captured = {}

    class FakeAdapter:
        async def send_interactive(self, **kw):
            captured.update(kw)
            return type("R", (), {"success": True, "message_id": "post_x", "error": None})()

    fake_runner = object()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mit, "_live_adapter", lambda p: (fake_runner, FakeAdapter()))
    monkeypatch.setattr(
        mit, "_dispatch_on_gateway_loop",
        lambda runner, make_coro, *a, **k: make_coro())
    tokens = sc.set_session_vars(thread_id="thr_root_1")
    try:
        out = mit.send_interactive_message({"target": "mattermost:chan_9", "text": "Pick", "buttons": []})
    finally:
        sc.clear_session_vars(tokens)
        monkeypatch.undo()
    res = json.loads(out)
    assert res.get("success") is True
    assert captured["reply_to"] == "thr_root_1"
    assert captured["chat_id"] == "chan_9"


def test_send_interactive_tool_inherits_dm_message_when_no_thread():
    """In a DM there is no thread root — the bot replies to the message being
    answered (message_id) instead of creating a brand-new root post."""
    from tools import mattermost_interactive_tools as mit
    import gateway.session_context as sc
    captured = {}

    class FakeAdapter:
        async def send_interactive(self, **kw):
            captured.update(kw)
            return type("R", (), {"success": True, "message_id": "post_x", "error": None})()

    fake_runner = object()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mit, "_live_adapter", lambda p: (fake_runner, FakeAdapter()))
    monkeypatch.setattr(
        mit, "_dispatch_on_gateway_loop",
        lambda runner, make_coro, *a, **k: make_coro())
    tokens = sc.set_session_vars(thread_id="", message_id="dm_last_post")
    try:
        out = mit.send_interactive_message({"target": "mattermost:chan_9", "text": "Pick", "buttons": []})
    finally:
        sc.clear_session_vars(tokens)
        monkeypatch.undo()
    res = json.loads(out)
    assert res.get("success") is True
    assert captured["reply_to"] == "dm_last_post"
    assert captured["chat_id"] == "chan_9"


# ---------------------------------------------------------------------------
# /model interactive picker (send_model_picker + bridge interact routing)
# ---------------------------------------------------------------------------

class TestMattermostModelPicker:

    def _adapter(self, **extra):
        from plugins.platforms.mattermost.adapter import MattermostAdapter, _with_mentions_disabled
        config = PlatformConfig(
            enabled=True, token="test-token",
            extra={"url": "https://mm.example.com", **extra},
        )
        a = MattermostAdapter(config)
        a._bot_user_id = "bot_id"
        a._session = object()  # satisfies the "connected" guard without aiohttp
        return a

    def _providers(self):
        return [
            {"slug": "openai", "name": "OpenAI", "models": ["openai/gpt-5.5", "openai/gpt-5.5-pro"],
             "total_models": 2},
            {"slug": "anthropic", "name": "Anthropic", "models": ["anthropic/claude-opus-4"],
             "total_models": 1},
        ]

    @pytest.mark.asyncio
    async def test_send_model_picker_posts_provider_menu(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "pick_1"})
        async def on_sel(chat, model, prov): return "switched"
        result = await a.send_model_picker(
            "chan_9", self._providers(), "openai/gpt-5.5", "openai",
            "sess_1", on_sel, metadata={})
        assert result.success is True
        assert result.message_id == "pick_1"
        # Provider selection is a select menu with a Cancel button, on one attachment.
        payload = a._api_post.call_args.args[1]
        assert payload["channel_id"] == "chan_9"
        assert payload["message"] == ""
        attach = payload["props"]["attachments"][0]
        actions = attach["actions"]
        provider = next(act for act in actions if act["id"] == "hmppro")
        assert provider["type"] == "select"
        assert provider["integration"]["url"] == "/plugins/hermes-bridge/interact"
        assert {o["value"] for o in provider["options"]} == {"openai", "anthropic"}
        assert any(act["id"] == "hmpcan" for act in actions)
        # State stored keyed by chat_id with the callback closure.
        assert "chan_9" in a._model_picker_state
        assert a._model_picker_state["chan_9"]["on_model_selected"] is on_sel

    @pytest.mark.asyncio
    async def test_bridge_interact_provider_selection_redraws_to_model_menu(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "pick_1"})
        a._api = AsyncMock(return_value={})
        await a.send_model_picker("chan_9", self._providers(), "", "openai", "sess", lambda *_: None, metadata={})
        a._api.reset_mock()
        a.handle_message = AsyncMock()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "hmppro", "selected_option": "anthropic",
            "post_id": "pick_1", "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "hmppro"}}}
        await a._handle_ws_event(evt)
        # Never surfaces as a user-visible TEXT turn.
        a.handle_message.assert_not_called()
        # Same post PUT-updated: now a model menu for the picked provider.
        put = a._api.call_args
        assert put.args[0] == "PUT" and put.args[1] == "posts/pick_1/patch"
        model_menu = next(act for act in put.args[2]["props"]["attachments"][0]["actions"]
                          if act["id"] == "hmpmod")
        assert {o["value"] for o in model_menu["options"]} == {"anthropic/claude-opus-4"}

    @pytest.mark.asyncio
    async def test_bridge_interact_model_selection_runs_switch(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "pick_1"})
        a._api = AsyncMock(return_value={})
        calls = []
        async def on_sel(chat, model, prov):
            calls.append((chat, model, prov))
            return "switched to gpt-5.5"
        await a.send_model_picker("chan_9", self._providers(), "", "openai", "sess", on_sel, metadata={})
        # Step 1: pick provider
        await a._handle_model_picker_callback({
            "action_id": "hmppro", "selected_option": "openai",
            "post_id": "pick_1", "channel_id": "chan_9"})
        a._api.reset_mock()
        # Step 2: pick model
        await a._handle_model_picker_callback({
            "action_id": "hmpmod", "selected_option": "openai/gpt-5.5",
            "post_id": "pick_1", "channel_id": "chan_9"})
        assert calls == [("chan_9", "openai/gpt-5.5", "openai")]
        # State closed + post finalized with the confirmation text and NO actions.
        assert "chan_9" not in a._model_picker_state
        put = a._api.call_args.args[2]
        attach = put["props"]["attachments"][0]
        assert attach["text"] == "switched to gpt-5.5"
        assert "actions" not in attach or attach["actions"] == []

    @pytest.mark.asyncio
    async def test_bridge_interact_cancel_closes_picker(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "pick_1"})
        a._api = AsyncMock(return_value={})
        await a.send_model_picker("chan_9", self._providers(), "", "", "sess", lambda *_: None, metadata={})
        await a._handle_model_picker_callback({
            "action_id": "hmpcan", "selected_option": "",
            "post_id": "pick_1", "channel_id": "chan_9"})
        assert "chan_9" not in a._model_picker_state
        put = a._api.call_args.args[2]
        assert put["props"]["attachments"][0]["actions"] == []

    @pytest.mark.asyncio
    async def test_bridge_interact_picker_with_no_state_is_dropped(self):
        a = self._adapter()
        a.handle_message = AsyncMock()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "hmpcan", "post_id": "pick_1",
            "channel_id": "chan_9", "user_id": "bob", "context": {}}}
        await a._handle_ws_event(evt)
        a.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bridge_interact_url_derives_from_plugin_path(self):
        a = self._adapter(bridge_plugin_path="plugins/hermes-bridge/config")
        assert a._bridge_interact_url() == "/plugins/hermes-bridge/interact"


class TestMattermostChoicePicker:

    def _adapter(self, **extra):
        from plugins.platforms.mattermost.adapter import MattermostAdapter
        config = PlatformConfig(
            enabled=True, token="test-token",
            extra={"url": "https://mm.example.com", **extra},
        )
        a = MattermostAdapter(config)
        a._bot_user_id = "bot_id"
        a._session = object()  # satisfies the "connected" guard
        return a

    def _choices(self):
        return [
            {"value": "none", "label": "Off", "is_current": False},
            {"value": "high", "label": "High", "is_current": True},
            {"value": "low", "label": "Low", "is_current": False},
        ]

    @pytest.mark.asyncio
    async def test_send_choice_picker_posts_select_menu(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "chp_1"})
        async def on_sel(chat, value): return f"applied {value}"
        result = await a.send_choice_picker("chan_9", "Reasoning", self._choices(),
                                            "sess", on_sel, metadata={})
        assert result.success is True
        assert result.message_id == "chp_1"
        payload = a._api_post.call_args.args[1]
        assert payload["message"] == ""
        attach = payload["props"]["attachments"][0]
        menu = next(act for act in attach["actions"] if act["id"] == "hchsel")
        assert menu["type"] == "select"
        assert {o["value"] for o in menu["options"]} == {"none", "high", "low"}
        labels = {o["text"] for o in menu["options"]}
        assert any("✓" in lbl and "High" in lbl for lbl in labels)  # is_current marked
        assert any(act["id"] == "hchcan" for act in attach["actions"])
        assert "chan_9" in a._choice_picker_state
        assert a._choice_picker_state["chan_9"]["on_choice_selected"] is on_sel

    @pytest.mark.asyncio
    async def test_bridge_interact_choice_selection_runs_apply(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "chp_1"})
        a._api = AsyncMock(return_value={})
        calls = []
        async def on_sel(chat, value):
            calls.append((chat, value))
            return "reasoning = high"
        await a.send_choice_picker("chan_9", "Reasoning", self._choices(), "sess", on_sel, metadata={})
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "hchsel", "selected_option": "high",
            "post_id": "chp_1", "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "hchsel"}}}
        a.handle_message = AsyncMock()
        await a._handle_ws_event(evt)
        # Picker click is not a user-visible TEXT turn.
        a.handle_message.assert_not_called()
        assert calls == [("chan_9", "high")]
        assert "chan_9" not in a._choice_picker_state
        put = a._api.call_args.args[2]
        assert put["props"]["attachments"][0]["text"] == "reasoning = high"
        assert put["props"]["attachments"][0]["actions"] == []

    @pytest.mark.asyncio
    async def test_bridge_interact_choice_cancel(self):
        a = self._adapter()
        a._api_post = AsyncMock(return_value={"id": "chp_1"})
        a._api = AsyncMock(return_value={})
        await a.send_choice_picker("chan_9", "Fast", self._choices(), "sess", lambda *_: None, metadata={})
        await a._handle_choice_picker_callback({
            "action_id": "hchcan", "selected_option": "",
            "post_id": "chp_1", "channel_id": "chan_9"})
        assert "chan_9" not in a._choice_picker_state
        assert a._api.call_args.args[2]["props"]["attachments"][0]["actions"] == []

    @pytest.mark.asyncio
    async def test_bridge_interact_choice_with_no_state_is_dropped(self):
        a = self._adapter()
        a.handle_message = AsyncMock()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "hchsel", "selected_option": "high", "post_id": "chp_1",
            "channel_id": "chan_9", "user_id": "bob", "context": {}}}
        await a._handle_ws_event(evt)
        a.handle_message.assert_not_called()


# ---------------------------------------------------------------------------
# Bridge: interactive dialogs (hermes_bridge_dialog WS event + send_dialog)
# ---------------------------------------------------------------------------

class TestMattermostBridgeDialog:

    def _adapter(self, **extra):
        from plugins.platforms.mattermost.adapter import MattermostAdapter
        config = PlatformConfig(
            enabled=True, token="test-token",
            extra={"url": "https://mm.example.com", **extra},
        )
        a = MattermostAdapter(config)
        a._bot_user_id = "bot_id"
        a._channel_type_code = AsyncMock(return_value="O")
        a.handle_message = AsyncMock()
        a._api_post = AsyncMock(return_value={"id": "post_9"})
        a._api = AsyncMock(return_value={})
        return a

    @pytest.mark.asyncio
    async def test_bridge_dialog_submit_builds_text_event(self):
        a = self._adapter()
        evt = {"event": "hermes_bridge_dialog", "data": {
            "callback_id": "report", "state": "qid_abc",
            "submission": {"summary": "All good", "priority": "high"},
            "cancelled": False,
            "user_id": "u_submit", "user_name": "sam", "channel_id": "chan_9",
        }}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()
        msg = a.handle_message.await_args.args[0]
        assert msg.message_type == MessageType.TEXT
        assert "All good" in msg.text
        assert msg.source.chat_id == "chan_9"
        assert msg.source.user_id == "u_submit"
        assert msg.raw_message["callback_id"] == "report"
        assert msg.raw_message["submission"]["priority"] == "high"
        assert msg.raw_message["response_for_question_id"] == "qid_abc"

    @pytest.mark.asyncio
    async def test_bridge_dialog_cancel_builds_text_event(self):
        a = self._adapter()
        evt = {"event": "hermes_bridge_dialog", "data": {
            "callback_id": "report", "state": "qid_abc",
            "submission": {}, "cancelled": True,
            "user_id": "u_submit", "user_name": "sam", "channel_id": "chan_9",
        }}
        await a._handle_ws_event(evt)
        msg = a.handle_message.await_args.args[0]
        assert "cancelled" in msg.text.lower()
        assert msg.raw_message["cancelled"] is True

    @pytest.mark.asyncio
    async def test_bridge_dialog_namespaced_prefix_still_hits(self):
        """Server namespaces plugin events: custom_<plugin_id>_<event>."""
        a = self._adapter()
        evt = {"event": "custom_hermes-bridge_hermes_bridge_dialog", "data": {
            "callback_id": "c", "submission": {"x": "1"},
            "cancelled": False, "user_id": "u", "channel_id": "chan_9",
        }}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bridge_dialog_missing_channel_dropped(self):
        a = self._adapter()
        await a._handle_ws_event({"event": "hermes_bridge_dialog", "data": {
            "callback_id": "c", "submission": {}, "cancelled": False}})
        a.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_dialog_posts_dialog_button(self):
        a = self._adapter()
        dialog = {"callback_id": "report", "title": "Report", "submit_label": "Send",
                  "elements": [{"name": "summary", "display_name": "Summary", "type": "textarea"}]}
        result = await a.send_dialog("chan_9", "Please report", dialog, question_id="qid_abc")
        assert result.success
        a._api_post.assert_awaited_once()
        payload = a._api_post.call_args.args[1]
        action = payload["props"]["attachments"][0]["actions"][0]
        assert action["id"] == "report"
        assert action["type"] == "button"
        # The dialog schema rides in integration.context under key "dialog".
        assert action["integration"]["context"]["dialog"]["callback_id"] == "report"
        assert action["integration"]["context"]["dialog"]["elements"][0]["type"] == "textarea"
        # question_id is both in the button context and used as dialog state.
        assert action["integration"]["context"]["question_id"] == "qid_abc"
        assert action["integration"]["context"]["dialog"]["state"] == "qid_abc"

    @pytest.mark.asyncio
    async def test_send_dialog_requires_schema(self):
        a = self._adapter()
        result = await a.send_dialog("chan_9", "text", {})
        assert not result.success
        a._api_post.assert_not_called()


# ---------------------------------------------------------------------------
# bridge interact: form_state + submit accumulation
# ---------------------------------------------------------------------------

class TestMattermostInteractFormState:

    def _adapter(self, **extra):
        from plugins.platforms.mattermost.adapter import MattermostAdapter
        config = PlatformConfig(
            enabled=True, token="test-token",
            extra={"url": "https://mm.example.com", **extra},
        )
        a = MattermostAdapter(config)
        a._bot_user_id = "bot_id"
        a._channel_type_code = AsyncMock(return_value="O")
        a.handle_message = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_interact_carries_form_state(self):
        """A normal click carries the accumulated form_state for the post."""
        a = self._adapter()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "size", "selected_option": "s", "post_id": "form_1",
            "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "size", "selected_option": "s"},
            "form_state": {"size": "s"},
        }}
        await a._handle_ws_event(evt)
        msg = a.handle_message.await_args.args[0]
        assert msg.raw_message["form_state"] == {"size": "s"}
        assert msg.raw_message.get("submission") is None
        assert msg.text == "s"  # choice text, not a fake command

    @pytest.mark.asyncio
    async def test_interact_submit_full_submission(self):
        """A submit click relays the whole accumulated form as submission text."""
        a = self._adapter()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "submit", "selected_option": "", "post_id": "form_1",
            "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "submit", "label": "Готово", "submit": True},
            "form_state": {"size": "s", "veggie": "yes"},
            "submission": {"size": "s", "veggie": "yes"},
        }}
        await a._handle_ws_event(evt)
        msg = a.handle_message.await_args.args[0]
        assert msg.raw_message["submission"] == {"size": "s", "veggie": "yes"}
        assert msg.raw_message["form_state"] == {"size": "s", "veggie": "yes"}
        assert "size=s" in msg.text and "veggie=yes" in msg.text

    @pytest.mark.asyncio
    async def test_interact_not_final_does_not_wake_model(self):
        """A click on one control of a multi-control form (final=false) must NOT
        wake the agent — the plugin already redrew the post with progress."""
        a = self._adapter()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "size", "selected_option": "s", "post_id": "form_1",
            "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "size"},
            "form_state": {"size": "s"},
            "final": False,
        }}
        await a._handle_ws_event(evt)
        a.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_interact_final_wakes_model(self):
        """A final click (single choice or submit) DOES wake the agent."""
        a = self._adapter()
        evt = {"event": "hermes_bridge_interact", "data": {
            "action_id": "yes", "selected_option": "", "post_id": "single_1",
            "channel_id": "chan_9", "user_id": "bob",
            "context": {"action_id": "yes", "label": "Да"},
            "form_state": {},
            "final": True,
        }}
        await a._handle_ws_event(evt)
        a.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_interactive_submit_button_sets_context_flag(self):
        """A button marked submit:true must carry submit:true in integration.context."""
        a = self._adapter()
        a._session = MagicMock()
        a._api_post = AsyncMock(return_value={"id": "p9"})
        a._api = AsyncMock(return_value={})
        result = await a.send_interactive(
            "chan_9", "Выбери всё",
            buttons=[{"id": "submit", "label": "Готово", "submit": True}],
            menu={"id": "size", "placeholder": "Размер", "options": [{"label": "S", "value": "s"}]})
        assert result.success
        submit_action = a._api_post.call_args.args[1]["props"]["attachments"][0]["actions"][0]
        assert submit_action["integration"]["context"].get("submit") is True
