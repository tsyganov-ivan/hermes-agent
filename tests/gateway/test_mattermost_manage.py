"""Tests for the Mattermost manage surface: delete/pin/unpin (and ephemeral-TTL unlock).

Mirrors the AsyncMock fake-session pattern from tests/gateway/test_mattermost.py;
reuses its `_make_adapter()` helper so the adapter is constructed exactly like the
existing suite's.
"""
import json
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


# ---------------------------------------------------------------------------
# F3: pin / unpin adapter methods
# ---------------------------------------------------------------------------

class TestMattermostPinUnpin:
    @pytest.mark.asyncio
    async def test_pin_message_posts_to_pin_endpoint(self, adapter):
        adapter._api_post = AsyncMock(return_value={"id": "post456"})
        r = await adapter.pin_message("ch123", "post456")
        adapter._api_post.assert_awaited_once_with("posts/post456/pin", {})
        assert r.get("success") is True

    @pytest.mark.asyncio
    async def test_pin_message_reports_failure(self, adapter):
        adapter._api_post = AsyncMock(return_value={})
        adapter._last_post_status = 403
        adapter._last_post_error = "forbidden"
        r = await adapter.pin_message("ch123", "post456")
        assert r.get("success") is False
        assert "forbidden" in r.get("error", "")

    @pytest.mark.asyncio
    async def test_pin_message_requires_message_id(self, adapter):
        adapter._api_post = AsyncMock()
        r = await adapter.pin_message("ch123", "")
        assert r.get("success") is False
        adapter._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unpin_message_calls_delete_pin_endpoint(self, adapter):
        adapter._api = AsyncMock(return_value={})
        r = await adapter.unpin_message("ch123", "post456")
        adapter._api.assert_awaited_once_with("DELETE", "posts/post456/pin")
        assert r.get("success") is True

    @pytest.mark.asyncio
    async def test_unpin_message_reports_40x(self, adapter):
        adapter._api = AsyncMock(return_value={})
        adapter._last_post_status = 404
        adapter._last_post_error = "not found"
        r = await adapter.unpin_message("ch123", "post456")
        assert r.get("success") is False


# ---------------------------------------------------------------------------
# F4: search_posts (team scoping)
# ---------------------------------------------------------------------------

class TestMattermostSearch:
    @pytest.mark.asyncio
    async def test_search_posts_uses_channel_team(self, adapter):
        adapter._api_get = AsyncMock(return_value={"team_id": "team1"})
        adapter._api_post = AsyncMock(return_value={"posts": {"p1": {}}, "order": ["p1"]})
        r = await adapter.search_posts("pepperoni", chat_id="ch1")
        assert r.get("success") is True
        url = adapter._api_post.call_args.args[0]
        assert url == "teams/team1/posts/search"
        assert adapter._api_post.call_args.args[1]["terms"] == "pepperoni"

    @pytest.mark.asyncio
    async def test_search_posts_with_explicit_team(self, adapter):
        adapter._api_post = AsyncMock(return_value={"posts": {"p1": {}}, "order": ["p1"]})
        r = await adapter.search_posts("x", team_id="teamX")
        assert adapter._api_post.call_args.args[0] == "teams/teamX/posts/search"
        assert r.get("success") is True

    @pytest.mark.asyncio
    async def test_search_posts_falls_back_to_instance_team(self, adapter):
        # channel has no team (DM) -> GET /teams returns the fallback.
        adapter._api_get = AsyncMock(side_effect=[{}, [{"id": "teamDef", "delete_at": 0}]])
        adapter._api_post = AsyncMock(return_value={"posts": {"p1": {}}, "order": ["p1"]})
        r = await adapter.search_posts("x", chat_id="dm1")
        assert r.get("success") is True
        assert adapter._api_post.call_args.args[0].startswith("teams/teamDef/")

    @pytest.mark.asyncio
    async def test_search_posts_error_when_no_team_and_dm(self, adapter):
        adapter._api_get = AsyncMock(return_value={})  # DM + no reachable team
        adapter._api_post = AsyncMock()
        r = await adapter.search_posts("x", chat_id="dm1")
        assert r.get("success") is False
        assert "team_id" in r.get("error", "")
        adapter._api_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_posts_requires_query(self, adapter):
        adapter._api_post = AsyncMock()
        r = await adapter.search_posts("", team_id="teamX")
        assert r.get("success") is False
        adapter._api_post.assert_not_awaited()


# ---------------------------------------------------------------------------
# F3 model tools (delete/pin/unpin) route through the live adapter
# ---------------------------------------------------------------------------

class TestMattermostManageTools:
    def _run_tool(self, tool_name, args, fake_adapter):
        import tools.mattermost_manage_tools as mmt

        captured = {}

        class FakeAdapter:
            async def delete_message(self, **kw):
                captured.setdefault("delete", []).append(kw)
                return True

            async def pin_message(self, **kw):
                captured.setdefault("pin", []).append(kw)
                return {"success": True, "id": "post456"}

            async def unpin_message(self, **kw):
                captured.setdefault("unpin", []).append(kw)
                return {"success": True}

            async def search_posts(self, **kw):
                captured.setdefault("search", []).append(kw)
                return {"success": True, "posts": {"p1": {}}}

        fake_runner = object()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mmt, "_live_adapter", lambda p: (fake_runner, fake_adapter or FakeAdapter()))
        monkeypatch.setattr(
            mmt, "_dispatch_on_gateway_loop",
            lambda runner, make_coro, *a, **k: make_coro())
        try:
            if tool_name == "delete_message":
                out = mmt.delete_message(args)
            elif tool_name == "pin_message":
                out = mmt.pin_message(args)
            elif tool_name == "search":
                out = mmt.search_mattermost_posts(args)
            else:
                out = mmt.unpin_message(args)
        finally:
            monkeypatch.undo()
        return captured, json.loads(out)

    def test_delete_message_tool_calls_adapter_exact_post(self):
        captured, res = self._run_tool(
            "delete_message", {"target": "mattermost:ch1", "message_id": "post9"}, None)
        assert res.get("success") is True
        assert captured["delete"] == [{"chat_id": "ch1", "message_id": "post9"}]

    def test_pin_message_tool_calls_adapter(self):
        captured, res = self._run_tool(
            "pin_message", {"target": "mattermost:ch1", "message_id": "post9"}, None)
        assert res.get("success") is True
        assert captured["pin"] == [{"chat_id": "ch1", "message_id": "post9"}]

    def test_unpin_message_tool_calls_adapter(self):
        captured, res = self._run_tool(
            "unpin_message", {"target": "mattermost:ch1", "message_id": "post9"}, None)
        assert res.get("success") is True
        assert captured["unpin"] == [{"chat_id": "ch1", "message_id": "post9"}]

    def test_search_mattermost_posts_tool_calls_adapter(self):
        captured, res = self._run_tool(
            "search", {"target": "mattermost:ch1", "query": "pepperoni", "page": 2, "per_page": 50}, None)
        assert res.get("success") is True
        assert captured["search"] == [
            {"query": "pepperoni", "chat_id": "ch1", "page": 2, "per_page": 50}]

    def test_search_tool_requires_query(self):
        _, res = self._run_tool("search", {"target": "mattermost:ch1"}, None)
        assert res.get("success") is not True
        assert "query" in res.get("error", "")

    def test_tools_require_message_id(self):
        for name in ("delete_message", "pin_message", "unpin_message"):
            _, res = self._run_tool(name, {"target": "mattermost:ch1"}, None)
            assert res.get("success") is not True  # tool_error returns error-only dict
            assert "message_id" in res.get("error", "")

    def test_tools_require_live_adapter(self):
        import tools.mattermost_manage_tools as mmt
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mmt, "_live_adapter", lambda p: (None, None))
        try:
            res = json.loads(mmt.delete_message({"target": "mattermost:ch1", "message_id": "post9"}))
        finally:
            monkeypatch.undo()
        assert res.get("success") is not True
        assert "adapter" in res.get("error", "")