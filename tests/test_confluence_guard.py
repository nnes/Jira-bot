"""Tests for Confluence read-only guardrails (Feature 1)."""
import pytest

from app.core.errors import ConfluenceForbidden
from app.graph.nodes.orchestrator import _is_confluence_mutate_request, orchestrator_node
from app.graph.state import empty_state
from app.integrations.confluence import reader as confluence_reader
from app.integrations.confluence.guards import assert_read_only


class TestAssertReadOnly:
    @pytest.mark.parametrize("op", ["read", "get", "fetch", "search", "view", "list"])
    def test_allowed_ops_pass(self, op):
        assert_read_only(op)  # should not raise

    @pytest.mark.parametrize("op", [
        "create", "update", "modify", "edit", "delete", "remove",
        "destroy", "purge", "archive", "trash", "move", "publish",
    ])
    def test_forbidden_ops_raise(self, op):
        with pytest.raises(ConfluenceForbidden):
            assert_read_only(op)

    def test_unknown_op_raises(self):
        with pytest.raises(ConfluenceForbidden):
            assert_read_only("frobnicate")


class TestReaderHasNoWriteMethods:
    def test_no_write_functions_exposed(self):
        forbidden = {"create", "update", "modify", "edit", "delete", "remove",
                     "destroy", "purge", "archive", "post", "put", "patch"}
        public = {n for n in dir(confluence_reader) if not n.startswith("_")}
        assert not (public & forbidden), f"reader exposes write funcs: {public & forbidden}"


class TestConfluenceMutateDetection:
    @pytest.mark.parametrize("text", [
        "xóa trang confluence X",
        "delete confluence page",
        "sửa trang wiki này",
        "remove confluence page",
        "chỉnh sửa wiki team",
        "edit confluence doc",
    ])
    def test_detects_mutate(self, text):
        assert _is_confluence_mutate_request(text) is True

    @pytest.mark.parametrize("text", [
        "tóm tắt trang confluence này",
        "tạo ticket từ trang confluence",
        "đọc confluence link này",
        "tạo story dựa trên confluence PRD",
    ])
    def test_ignores_read_and_create_from(self, text):
        assert _is_confluence_mutate_request(text) is False


@pytest.mark.asyncio
async def test_orchestrator_refuses_confluence_mutate():
    state = empty_state()
    state["messages"] = [{"role": "user", "content": "xóa trang confluence ABC"}]
    result = await orchestrator_node(state)
    reply = result["messages"][-1]["content"]
    assert "read-only" in reply.lower() or "không được phép" in reply.lower()
    assert result["ready_to_generate"] is False
    assert result["ready_to_update"] is False
