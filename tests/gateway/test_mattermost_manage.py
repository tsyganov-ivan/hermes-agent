"""Tests for the Mattermost manage surface: delete/pin/unpin (and ephemeral-TTL unlock).

Mirrors the AsyncMock fake-session pattern from tests/gateway/test_mattermost.py;
reuses its `_make_adapter()` helper so the adapter is constructed exactly like the
existing suite's.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.gateway.test_mattermost import _make_adapter


@pytest.fixture
def adapter():
    a = _make_adapter()
    a._session = MagicMock()
    return a


def _fake_http_response(status: int = 200, json_payload=None, body: str = ""):
    """A minimal aiohttp-like context-manager response for self._session.<verb>()."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_payload or {})
    mock_resp.text = AsyncMock(return_value=body)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# F1: delete_message (DELETE /posts/{id}) -> bool
# ---------------------------------------------------------------------------

class TestMattermostDeleteMessage:
    @pytest.mark.asyncio
    async def test_delete_message_calls_delete_endpoint_and_returns_true(self, adapter):
        resp = _fake_http_response(status=200)
        adapter._session.delete = MagicMock(return_value=resp)

        ok = await adapter.delete_message("ch123", "post456")

        assert ok is True
        call = adapter._session.delete.call_args
        assert call[0][0].endswith("/api/v4/posts/post456")

    @pytest.mark.asyncio
    async def test_delete_message_returns_false_on_40x(self, adapter):
        resp = _fake_http_response(status=403, body="forbidden")
        adapter._session.delete = MagicMock(return_value=resp)

        ok = await adapter.delete_message("ch123", "post456")

        assert ok is False

    @pytest.mark.asyncio
    async def test_delete_message_requires_message_id(self, adapter):
        ok = await adapter.delete_message("ch123", "")
        assert ok is False

    @pytest.mark.asyncio
    async def test_delete_message_is_not_the_base_placeholder(self, adapter):
        """Once overridden, core _unwrap_ephemeral stops forcing Ephemeral TTL to 0."""
        from gateway.platforms.base import BasePlatformAdapter
        assert type(adapter).delete_message is not BasePlatformAdapter.delete_message