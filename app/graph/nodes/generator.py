import json
import logging
import re
from typing import Any, Dict, Optional

from app.config import settings
from app.core.errors import JiraError, JiraFieldError, JiraIssueNotFound
from app.core.pii import mask_pii
from app.graph.state import AgentState
from app.llm.client import get_llm_client
from app.llm.registry import ModelRole, get_model
from app.prompts.generator import GENERATOR_SYSTEM_PROMPT
from app.schemas.ticket import JiraTicket, TicketType

logger = logging.getLogger(__name__)


# ── JSON helpers ─────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Dict[str, Any]:
    """Parse JSON from raw LLM output, handling markdown code blocks."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


# ── Reply formatters ─────────────────────────────────────────────────────────

def _reply_mock(ticket: Dict[str, Any]) -> str:
    if not ticket:
        return "⚠️ Không thể parse ticket JSON từ model output."
    pretty = json.dumps(ticket, ensure_ascii=False, indent=2)
    return (
        "✅ **Ticket JSON đã sẵn sàng** (mock mode — chưa tạo trên Jira):\n\n"
        f"```json\n{pretty}\n```\n\n"
        "_Để tạo ticket thật, set `USE_MOCK_JIRA=false` trong `.env`._"
    )


def _reply_created(issue_key: str, ticket: Dict[str, Any]) -> str:
    link = f"{settings.jira_server_url.rstrip('/')}/browse/{issue_key}"
    summary = ticket.get("summary", "")
    return (
        f"✅ **Ticket đã được tạo thành công!**\n\n"
        f"📋 `{issue_key}` — {summary}\n"
        f"🔗 {link}"
    )


def _reply_jira_error(err: str, ticket: Dict[str, Any]) -> str:
    pretty = json.dumps(ticket, ensure_ascii=False, indent=2)
    return (
        f"⚠️ **Jira trả về lỗi** — ticket chưa được tạo:\n`{err}`\n\n"
        f"Ticket JSON đã chuẩn bị sẵn:\n```json\n{pretty}\n```"
    )


# ── Jira creation helper ─────────────────────────────────────────────────────

async def _create_on_jira(
    ticket_dict: Dict[str, Any],
    requester_email: str = "",
) -> Optional[str]:
    """Attempt to create the ticket on Jira with one auto-sync retry on field errors.

    *requester_email*: Teams user's email. When jira_set_reporter_from_teams is True
    and bot PAT mode is active, this resolves to a Jira username set as reporter.
    Falls back to the bot's own username if email is unknown or not found in Jira.
    """
    from app.integrations.jira.client import get_jira_client
    from app.integrations.jira.field_sync import auto_sync

    jira = get_jira_client()
    project_key = ticket_dict.get("project_key", "")
    issue_type = ticket_dict.get("issue_type", "")

    # Verify Epic exists before linking (Story / Task only)
    epic_link = ticket_dict.get("epic_link")
    if epic_link and issue_type in ("Story", "Task"):
        try:
            await jira.get_issue(epic_link)
        except JiraIssueNotFound:
            raise JiraIssueNotFound(epic_link)

    # Resolve sprint label to sprint id (best-effort, set via Agile API after create)
    sprint_id: Optional[int] = None
    sprint_label = ticket_dict.get("sprint")
    if sprint_label and issue_type in ("Story", "Task"):
        sprint_id = await jira.resolve_sprint_id(project_key, sprint_label)

    # Resolve reporter: Teams user → bot username → omit (Jira defaults to bot)
    reporter_name: Optional[str] = None
    if settings.jira_set_reporter_from_teams and not settings.jira_user_email:
        if requester_email:
            reporter_name = await jira.get_jira_username_by_email(requester_email)
            if reporter_name is None:
                logger.warning(
                    "_create_on_jira: Jira user not found for '%s' — falling back to bot username",
                    requester_email,
                )
        if reporter_name is None:
            reporter_name = await jira.get_bot_username()

    try:
        issue_key = await jira.create_issue(
            jira.build_fields(ticket_dict, reporter_name=reporter_name)
        )
    except JiraFieldError as exc:
        reporter_error = "reporter" in (exc.errors or {})
        only_reporter_error = reporter_error and set(exc.errors.keys()) == {"reporter"}

        if only_reporter_error:
            # Bot lacks "Modify Reporter" permission — retry without reporter field
            logger.warning(
                "_create_on_jira: bot lacks 'Modify Reporter' permission — "
                "retrying without reporter (Jira will default to bot account). errors=%s",
                exc.errors,
            )
            issue_key = await jira.create_issue(jira.build_fields(ticket_dict))

        elif reporter_error:
            # Reporter error mixed with other field errors — strip reporter + auto-sync
            logger.warning(
                "_create_on_jira: reporter error mixed with field errors — "
                "stripping reporter and auto-syncing. errors=%s",
                exc.errors,
            )
            await auto_sync(project_key)
            issue_key = await jira.create_issue(jira.build_fields(ticket_dict))

        else:
            # Pure field error (wrong customfield IDs) — auto-sync and retry with reporter
            logger.warning(
                "Field error on first attempt (%s) — auto-syncing fields for project '%s'",
                exc.errors, project_key,
            )
            await auto_sync(project_key)
            issue_key = await jira.create_issue(
                jira.build_fields(ticket_dict, reporter_name=reporter_name)
            )

    # Move the new issue into its sprint (best-effort — never fail a created ticket)
    if issue_key and sprint_id is not None:
        try:
            await jira.move_issue_to_sprint(sprint_id, issue_key)
            logger.info("generator: moved %s into sprint id %s", issue_key, sprint_id)
        except Exception as exc:
            logger.warning("generator: could not move %s into sprint %s — %s", issue_key, sprint_id, exc)

    return issue_key


# ── Main node ────────────────────────────────────────────────────────────────

async def generator_node(state: AgentState) -> Dict[str, Any]:
    client = get_llm_client()
    model = get_model(ModelRole.GENERATOR)
    messages = state["messages"]
    slots = state.get("slots", {})
    confluence_data = state.get("confluence_data") or ""

    # ── 1. Build LLM prompt (mask PII before sending to LLM) ─────────────────
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {mask_pii(m['content'])}" for m in messages[-20:]
    )
    user_prompt_parts = [
        "CONVERSATION:\n" + conversation_text,
        "\nCOLLECTED SLOTS:\n" + json.dumps(slots, ensure_ascii=False, indent=2),
    ]
    if confluence_data:
        # Safety cap — reranker already limits output but guard here for direct calls
        if len(confluence_data) > 15_000:
            confluence_data = confluence_data[:15_000]
            logger.warning("generator: confluence_data truncated to 15,000 chars")
        user_prompt_parts.append("\nCONFLUENCE CONTEXT:\n" + mask_pii(confluence_data))
    user_prompt_parts.append("\nTạo Jira ticket JSON theo schema. Trả về JSON only.")

    # ── 2. Call LLM (qwen3-5-27b) ────────────────────────────────────────────
    ticket_dict: Dict[str, Any] = {}
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": GENERATOR_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_prompt_parts)},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        ticket_dict = _extract_json(raw)

        # Mask any PII the model may have hallucinated into the ticket fields
        for key in ("summary", "description", "acceptance_criteria"):
            if isinstance(ticket_dict.get(key), str):
                ticket_dict[key] = mask_pii(ticket_dict[key])

        try:
            JiraTicket(**ticket_dict)
        except Exception as val_err:
            logger.warning("Ticket schema validation warning: %s", val_err)

    except Exception as exc:
        logger.error("Generator LLM failed: %s", exc, exc_info=True)
        reply = "Xin lỗi, không thể tạo ticket lúc này. Vui lòng thử lại."
        return {
            **state,
            "messages": [*messages, {"role": "assistant", "content": reply}],
            "ready_to_generate": False,
            "draft_shown": False,
        }

    # ── 3. Create on Jira (or mock) ──────────────────────────────────────────
    issue_key: Optional[str] = None
    reply: str

    if settings.use_mock_jira:
        reply = _reply_mock(ticket_dict)
    else:
        from app.core.authz import get_user_email
        requester_email = get_user_email(state)
        try:
            issue_key = await _create_on_jira(ticket_dict, requester_email=requester_email)
            reply = _reply_created(issue_key, ticket_dict)
        except JiraIssueNotFound as exc:
            logger.warning("Epic not found during ticket creation: %s", exc)
            reply = (
                f"⚠️ Epic `{exc.issue_key}` không tồn tại trên Jira.\n"
                "Vui lòng kiểm tra lại Epic ID và thử lại."
            )
        except JiraError as exc:
            logger.error("Jira create_issue failed: %s", exc, exc_info=True)
            reply = _reply_jira_error(str(exc), ticket_dict)
        except Exception as exc:
            logger.error("Unexpected error during Jira creation: %s", exc, exc_info=True)
            reply = (
                "⚠️ Đã xảy ra lỗi không mong đợi khi tạo ticket. "
                "Ticket chưa được tạo — vui lòng thử lại."
            )

    return {
        **state,
        "messages": [*messages, {"role": "assistant", "content": reply}],
        "ticket_json": ticket_dict or state.get("ticket_json"),
        "jira_issue_key": issue_key,
        "ready_to_generate": False,
        "draft_shown": False,
    }
