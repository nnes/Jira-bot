"""Updater node — execute a confirmed UPDATE on an existing Jira ticket.

Reached only when state["ready_to_update"] is True (user confirmed the UPDATE DRAFT).
Uses the GENERATOR model to extract a change-set JSON from the conversation, then
calls the confirmed update path (_update_issue_confirmed) on the Jira client.

This is the post-confirmation path the require_update_confirmation guard is designed
for: the bot collected the user's "có/yes" before we reach here.
"""
import json
import logging
import re
from typing import Any, Dict, Optional

from app.config import settings
from app.core.errors import JiraError, JiraIssueNotFound
from app.graph.nodes.generator import _extract_json
from app.graph.state import AgentState
from app.llm.client import get_llm_client
from app.llm.registry import ModelRole, get_model
from app.prompts.updater import UPDATER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def _reply_updated(issue_key: str, changes: Dict[str, Any]) -> str:
    link = f"{settings.jira_server_url.rstrip('/')}/browse/{issue_key}"
    pretty = json.dumps(changes, ensure_ascii=False, indent=2)
    return (
        f"✅ **Ticket đã được cập nhật thành công!**\n\n"
        f"📋 `{issue_key}`\n"
        f"🔗 {link}\n\n"
        f"Thay đổi đã áp dụng:\n```json\n{pretty}\n```"
    )


async def updater_node(state: AgentState) -> Dict[str, Any]:
    client = get_llm_client()
    model = get_model(ModelRole.GENERATOR)
    messages = state["messages"]

    reset = {"ready_to_update": False, "update_draft_shown": False}

    # ── 1. Extract change-set JSON from the conversation ─────────────────────
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages[-20:]
    )
    jira_context = state.get("jira_context") or ""
    user_prompt = "CONVERSATION:\n" + conversation_text
    if jira_context:
        user_prompt += "\n\nJIRA CONTEXT:\n" + jira_context
    user_prompt += "\n\nTrích xuất issue_key + changes. Trả về JSON only."

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": UPDATER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        parsed = _extract_json(raw)
    except Exception as exc:
        logger.error("Updater LLM failed: %s", exc, exc_info=True)
        reply = "Xin lỗi, không thể xử lý yêu cầu cập nhật lúc này. Vui lòng thử lại."
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    issue_key = (parsed.get("issue_key") or "").strip()
    changes = parsed.get("changes") or {}

    if not issue_key or not changes:
        reply = (
            "⚠️ Tôi chưa xác định rõ ticket hoặc thay đổi cần áp dụng. "
            "Bạn vui lòng nói rõ mã ticket và field muốn cập nhật."
        )
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    # ── 2. Build & apply the update via the confirmed path ───────────────────
    from app.integrations.jira.client import get_jira_client

    jira = get_jira_client()
    reply: str

    if settings.use_mock_jira:
        pretty = json.dumps({"issue_key": issue_key, "changes": changes}, ensure_ascii=False, indent=2)
        reply = (
            "✅ **Update sẵn sàng** (mock mode — chưa gọi Jira):\n\n"
            f"```json\n{pretty}\n```"
        )
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    try:
        # Verify the ticket exists first
        await jira.get_issue(issue_key)

        m = re.match(r"([A-Za-z][A-Za-z0-9_]+)-\d+", issue_key)
        project_key = m.group(1) if m else issue_key.split("-")[0]

        # Sprint is handled via the Agile API (the Sprint field is usually not on the
        # edit screen, so it can't be set through the issue-field PUT). Split it out.
        sprint_label = changes.get("sprint")
        field_changes = {k: v for k, v in changes.items() if k != "sprint"}

        applied: Dict[str, Any] = {}
        sprint_unresolved: Optional[str] = None

        # 1. Regular field updates via the issue-field PUT
        fields = jira.build_update_fields(field_changes)
        if fields:
            await jira._update_issue_confirmed(issue_key, fields)
            applied.update(field_changes)

        # 2. Sprint (re)assignment via the Agile API
        if sprint_label:
            sprint_id = await jira.resolve_sprint_id(project_key, sprint_label)
            if sprint_id is not None:
                await jira.move_issue_to_sprint(sprint_id, issue_key)
                applied["sprint"] = sprint_label
            else:
                sprint_unresolved = str(sprint_label)

        if not applied:
            if sprint_unresolved:
                reply = (
                    f"⚠️ Không tìm thấy sprint `{sprint_unresolved}` trên board của project "
                    f"`{project_key}`. Kiểm tra lại tên sprint (định dạng `XXXX YY.MM.A/B/C`), "
                    "hoặc dùng 'Active Sprint' / 'Next Sprint'."
                )
            else:
                reply = "⚠️ Không có field hợp lệ nào để cập nhật."
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

        reply = _reply_updated(issue_key, applied)
        if sprint_unresolved:
            reply += (
                f"\n\n⚠️ Lưu ý: không gán được sprint `{sprint_unresolved}` "
                f"(không tìm thấy trên board của `{project_key}`)."
            )
    except JiraIssueNotFound:
        reply = f"⚠️ Ticket `{issue_key}` không tồn tại trên Jira. Vui lòng kiểm tra lại mã."
    except JiraError as exc:
        logger.error("Jira update failed: %s", exc, exc_info=True)
        reply = f"⚠️ Cập nhật ticket thất bại: {exc}"
    except Exception as exc:
        logger.error("Unexpected error during Jira update: %s", exc, exc_info=True)
        reply = "⚠️ Đã xảy ra lỗi không mong đợi khi cập nhật ticket. Vui lòng thử lại."

    return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}
