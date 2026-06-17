"""Tests for delete-request detection + refusal in the orchestrator (Issue 2)."""
import pytest

from app.graph.nodes.orchestrator import _is_delete_request, orchestrator_node
from app.graph.state import empty_state


class TestIsDeleteRequest:
    @pytest.mark.parametrize("text", [
        "xóa ticket EWL-123",
        "xoá ticket EWL-123",
        "delete issue PCFBANK-9988",
        "hủy story này",
        "remove task EWL-7",
        "Xóa Epic EWL-1 giúp tôi",
        "delete EWL-99",
        "purge ticket ABC-1",
    ])
    def test_detects_delete_intent(self, text):
        assert _is_delete_request(text) is True

    @pytest.mark.parametrize("text", [
        "cập nhật story point EWL-1 thành 5",
        "tóm tắt trang confluence này",
        "tạo story mới cho Payment",
        "tạo ticket xóa cache Redis",       # delete verb is ticket CONTENT, not intent
        "tạo task xóa dữ liệu tạm",
        "đổi assignee của EWL-3",
    ])
    def test_ignores_non_delete(self, text):
        assert _is_delete_request(text) is False


@pytest.mark.asyncio
async def test_orchestrator_refuses_delete_without_llm_call():
    """A delete request must short-circuit before any LLM/API call and not set ready flags."""
    state = empty_state()
    state["messages"] = [{"role": "user", "content": "xóa ticket EWL-123"}]

    result = await orchestrator_node(state)

    # Last assistant message is a refusal
    assert result["messages"][-1]["role"] == "assistant"
    reply = result["messages"][-1]["content"]
    assert "không được phép xóa" in reply.lower() or "🚫" in reply
    # No flow triggered
    assert result["ready_to_generate"] is False
    assert result["ready_to_update"] is False
