import logging
import re
from typing import Any, Dict, Optional, Set

from app.core.errors import ConfluenceUnavailable
from app.graph.state import AgentState
from app.llm.client import get_llm_client
from app.llm.registry import ModelRole, get_model
from app.prompts.orchestrator import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_CONFIRM_KEYWORDS: Set[str] = {
    "có", "yes", "xác nhận", "ok", "đồng ý",
    "tạo đi", "tạo ticket", "create", "confirm", "proceed",
}

# Keywords that signal the create draft was shown (matches the prompt's BƯỚC 3 format)
_DRAFT_MARKERS = ("TICKET DRAFT", "📋", "Bạn xác nhận tạo ticket")

# Keywords that signal an UPDATE draft (diff) was shown (Issue 1b)
_UPDATE_DRAFT_MARKERS = ("UPDATE DRAFT", "✏️", "Bạn xác nhận cập nhật")

# Detect a Confluence URL: hostname contains "confluence" or "wiki" as common patterns
_CONFLUENCE_URL_RE = re.compile(
    r"https?://[^\s]*(?:confluence|wiki)[^\s]*", re.IGNORECASE
)

# Detect a Jira browse URL: https://<host>/browse/PROJECT-123
# Also matches bare issue keys like PCFBANK-9988 mentioned standalone
_JIRA_URL_RE = re.compile(
    r"https?://[^\s]+/browse/([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE
)
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

# Detect self-assign: "gán cho tôi", "assign cho mình", "tôi tự nhận", "tôi làm", ...
_SELF_ASSIGN_RE = re.compile(
    r"(?:"
    r"(?:gán|assign|phân\s*công)[\s\S]{0,25}?\b(?:cho\s*)?(?:tôi|mình|me)\b"
    r"|(?:tôi|mình)\s+(?:tự\s*nhận|làm|take|pick\s*up|sẽ\s*làm)"
    r"|assign\s+(?:to\s+)?me\b"
    r")",
    re.IGNORECASE,
)


def _is_self_assign(text: str) -> bool:
    return bool(_SELF_ASSIGN_RE.search(text))


# Detect a DELETE request: a delete verb FOLLOWED CLOSELY by a ticket reference.
# Requiring forward proximity avoids false positives on ticket *content* like
# "tạo ticket xóa cache Redis" (here the ticket noun precedes the verb).
_DELETE_VERB = r"(?:x[oó][aá]|delete|remove|h[uủ][yý]|purge|drop)"

_DELETE_REQUEST_RE = re.compile(
    _DELETE_VERB
    + r"[\s\w]{0,25}?"                                            # small same-clause gap
    r"(?:ticket|issue|epic|stor(?:y|ies)|task|sub-?task|[A-Za-z][A-Za-z0-9_]+-\d+)",
    re.IGNORECASE,
)

# Confluence delete/modify request: a mutate verb → confluence/wiki/page reference.
# Excludes "tạo"/"update" so legitimate "tạo ticket từ trang confluence" is not blocked.
_CONFLUENCE_MUTATE_RE = re.compile(
    r"(?:x[oó][aá]|delete|remove|h[uủ][yý]|s[uử]a|ch[iỉ]nh\s*s[uử]a|edit|modify)"
    r"[\s\w]{0,20}?"
    r"(?:confluence|wiki)",
    re.IGNORECASE,
)


def _is_delete_request(text: str) -> bool:
    """True if the message asks to delete a Jira ticket (delete verb → ticket ref)."""
    return bool(_DELETE_REQUEST_RE.search(text))


def _is_confluence_mutate_request(text: str) -> bool:
    """True if the message asks to delete/modify/create a Confluence page (read-only violation)."""
    return bool(_CONFLUENCE_MUTATE_RE.search(text))


# Detect a statistics/aggregation request: a stats phrase + a Jira-domain noun.
_STATS_INTENT_RE = re.compile(
    r"(th[oố]ng\s*k[eê]|t[oổ]ng\s*h[oợ]p|b[aá]o\s*c[aá]o|report|statistic|metric|"
    r"s[oố]\s*l[uư][oợ]ng|bao\s*nhi[eê]u|c[oó]\s*m[aấ]y|how\s*many|\bcount\b|"
    r"th[aà]nh\s*vi[eê]n|member|participant|contributor|ai\s+(?:trong|tham\s+gia)|"
    r"danh\s*s[aá]ch\s*(?:user|ng[uư][oờ]i|member)|list\s+(?:user|member|team))",
    re.IGNORECASE,
)
_STATS_DOMAIN_RE = re.compile(
    r"(ticket|issue|epic|stor(?:y|ies)|task|bug|point|sprint|project|d[uự]\s*[aá]n|team)",
    re.IGNORECASE,
)
# A create verb means the user is building a ticket — not asking for stats, even if
# the ticket content mentions "báo cáo"/"thống kê".
_CREATE_VERB_RE = re.compile(r"(?:^|\b)(?:t[aạ]o|create|m[oở]\s+ticket|new\s+ticket)\b", re.IGNORECASE)


_SPRINT_KEYWORD_RE = re.compile(
    r"\b(?:sprint|active\s+sprint|next\s+sprint|sprint\s+hi[eệ]n\s+t[aạ]i|"
    r"sprint\s+ti[eế]p|sprint\s+k[eế]\s+ti[eế]p|sprint\s+t[oớ]i|"
    r"chuy[eể]n\s+sprint|[dđ][aặ]t\s+sprint|move.*sprint|update.*sprint)\b",
    re.IGNORECASE,
)


# Match "project PCFBANK" or "dự án EWL" patterns in free text
_PROJECT_MENTION_RE = re.compile(
    r"(?:project|d[uự]\s*[aá]n)\s+([A-Z][A-Z0-9]{1,11})\b",
    re.IGNORECASE,
)


def _extract_project_key_from_text(text: str) -> Optional[str]:
    """Return a project key from text: 'project PCFBANK', issue key, or None."""
    m = _PROJECT_MENTION_RE.search(text)
    if m:
        return m.group(1).upper()
    m = _JIRA_KEY_RE.search(text)
    if m:
        return m.group(1).rsplit("-", 1)[0]
    return None


def _needs_sprint_context(text: str, slots: Dict[str, Any]) -> bool:
    """True when we should fetch real sprint data to inject into the prompt.

    Triggered when the user's message mentions sprint AND we can derive a project key
    from: the filled slots, a Jira issue key (PROJECT-123), or a 'project X' mention.
    """
    if not _SPRINT_KEYWORD_RE.search(text):
        return False
    return bool(slots.get("project_key") or _extract_project_key_from_text(text))


def _is_stats_request(text: str) -> bool:
    """True if the message asks for Jira statistics (stats phrase + domain noun).

    Suppressed when a create verb is present (the user is creating a ticket whose
    content happens to mention reporting/aggregation).
    """
    if _CREATE_VERB_RE.search(text):
        return False
    return bool(_STATS_INTENT_RE.search(text) and _STATS_DOMAIN_RE.search(text))

# Minimax (and some other models) may emit proprietary tool-call XML even when
# no tools are registered.  Strip all such blocks before surfacing the reply.
_TOOL_CALL_RE = re.compile(
    r"<(?:minimax:)?tool_call\b[^>]*>.*?</(?:minimax:)?tool_call>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_CALL_RE = re.compile(
    r"<function_calls?>.*?</function_calls?>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_tool_calls(text: str) -> str:
    """Remove any tool-call XML the model may have hallucinated.

    Returns the cleaned text (leading/trailing whitespace removed).
    Call _has_tool_calls() first to check whether stripping actually happened.
    """
    text = _TOOL_CALL_RE.sub("", text)
    text = _FUNCTION_CALL_RE.sub("", text)
    return text.strip()


def _has_tool_calls(text: str) -> bool:
    """Return True if *text* contains tool-call XML patterns."""
    return bool(_TOOL_CALL_RE.search(text) or _FUNCTION_CALL_RE.search(text))


def _extract_confluence_url(text: str) -> Optional[str]:
    """Return the first Confluence URL found in *text*, or None.

    Detection strategy (in order):
    1. URL whose hostname/path contains 'confluence' or 'wiki' (common Confluence patterns).
    2. URL that starts with the configured CONFLUENCE_SERVER_URL (catches custom hostnames).
    """
    m = _CONFLUENCE_URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;)>")

    # Fallback: match any HTTP URL that shares the configured Confluence host
    from app.config import settings
    base = settings.confluence_server_url.rstrip("/")
    if base and "localhost" not in base:
        host = re.escape(base.split("//", 1)[-1].split("/")[0])
        dyn_re = re.compile(rf"https?://{host}[^\s]*", re.IGNORECASE)
        m2 = dyn_re.search(text)
        if m2:
            return m2.group(0).rstrip(".,;)>")

    return None


def _extract_jira_issue_key(text: str) -> Optional[str]:
    """Return issue key from a Jira browse URL or a bare key mention, or None."""
    m = _JIRA_URL_RE.search(text)
    if m:
        return m.group(1).upper()
    # Bare key only if the text does NOT look like it already has a Confluence URL
    # (to avoid false-positive matching PROJECT-1 inside Confluence page paths)
    if not _CONFLUENCE_URL_RE.search(text):
        m = _JIRA_KEY_RE.search(text)
        if m:
            return m.group(1).upper()
    return None


# Greenhopper sprint field can serialise as objects or legacy toString blobs:
#   {"name": "PCF-BANK 26.05.B", "state": "closed", ...}
#   "com.atlassian.greenhopper.service.sprint.Sprint@x[id=1,state=CLOSED,name=PCF-BANK 26.05.B,...]"
_SPRINT_STR_NAME_RE = re.compile(r"name=([^,\]]+)")
_SPRINT_STR_STATE_RE = re.compile(r"state=([^,\]]+)")


def _parse_sprint_entry(entry: Any):
    """Return (name, state) from a single sprint field entry (dict or legacy string)."""
    if isinstance(entry, dict):
        return entry.get("name"), (entry.get("state") or "").lower()
    if isinstance(entry, str):
        nm = _SPRINT_STR_NAME_RE.search(entry)
        st = _SPRINT_STR_STATE_RE.search(entry)
        return (nm.group(1).strip() if nm else None), (st.group(1).strip().lower() if st else "")
    return None, None


def _looks_like_sprint_value(item: Any) -> bool:
    if isinstance(item, dict):
        return "name" in item and "state" in item
    if isinstance(item, str):
        return "greenhopper" in item or ("name=" in item and "state=" in item)
    return False


def _extract_sprints(fields: Dict[str, Any], sprint_field_id: str):
    """Extract an ordered list of (name, state) from the issue's sprint field.

    Tries the configured field id first, then scans custom fields for sprint-like
    data so a misconfigured field id doesn't hide multi-sprint history.
    """
    raw = fields.get(sprint_field_id)
    if not raw:
        for k, v in fields.items():
            if not k.startswith("customfield_"):
                continue
            if isinstance(v, list) and v and _looks_like_sprint_value(v[0]):
                raw = v
                break
            if _looks_like_sprint_value(v):
                raw = v
                break
    if not raw:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out = []
    for entry in raw:
        name, state = _parse_sprint_entry(entry)
        if name:
            out.append((name, state))
    return out


def _format_jira_issue(issue: Dict[str, Any], sprint_field_id: str = "") -> str:
    """Convert a Jira REST API issue response into readable plain text."""
    key = issue.get("key", "")
    fields = issue.get("fields", {})
    lines = [f"=== Jira Issue: {key} ==="]

    for label, getter in [
        ("Summary",     lambda f: f.get("summary", "")),
        ("Type",        lambda f: (f.get("issuetype") or {}).get("name", "")),
        ("Status",      lambda f: (f.get("status") or {}).get("name", "")),
        ("Priority",    lambda f: (f.get("priority") or {}).get("name", "")),
        ("Assignee",    lambda f: (f.get("assignee") or {}).get("displayName", "Unassigned")),
        ("Labels",      lambda f: ", ".join(f.get("labels") or []) or "—"),
    ]:
        value = getter(fields)
        if value:
            lines.append(f"{label}: {value}")

    # Sprint(s) — a ticket may span multiple sprints; list them all in order.
    sprints = _extract_sprints(fields, sprint_field_id)
    if sprints:
        rendered = ", ".join(
            f"{name} ({state})" if state else name for name, state in sprints
        )
        lines.append(f"Sprint: {rendered}")
        current = [name for name, state in sprints if state == "active"]
        if current:
            lines.append("Sprint hiện tại (active): " + ", ".join(current))

    desc = fields.get("description") or ""
    if desc:
        # Jira description can be long — trim to 3000 chars
        lines.append("\nDescription:\n" + desc[:3000])

    return "\n".join(lines)


async def _fetch_jira_issue(issue_key: str) -> Optional[str]:
    """Fetch a Jira issue and return formatted plain text, or None on error."""
    from app.config import settings
    from app.core.errors import JiraError, JiraIssueNotFound
    from app.integrations.jira.client import get_jira_client

    # Skip if Jira isn't configured (still at localhost default)
    if "localhost" in settings.jira_server_url and not settings.jira_api_token:
        logger.debug("orchestrator: Jira not configured, skipping fetch for %s", issue_key)
        return None
    try:
        jira = get_jira_client()
        issue = await jira.get_issue(issue_key)
        content = _format_jira_issue(issue, sprint_field_id=jira._sprint_field)
        logger.info("orchestrator: fetched Jira issue %s (%d chars)", issue_key, len(content))
        return content
    except JiraIssueNotFound:
        logger.warning("orchestrator: Jira issue %s not found", issue_key)
        return None
    except JiraError as exc:
        logger.warning("orchestrator: Jira fetch error for %s — %s", issue_key, exc)
        return None
    except Exception as exc:
        logger.error("orchestrator: unexpected Jira fetch error — %s", exc, exc_info=True)
        return None


_MOCK_CONFLUENCE_CONTENT = """
# [MOCK] Payment Gateway — Onboarding API PRD

## 1. Overview
Payment Gateway Onboarding API cho phép merchant tích hợp thanh toán qua ZaloPay.
URL: confluence.zalopay.vn (mock mode — không kết nối được mạng nội bộ)

## 2. Business Context
- Merchant cần onboard nhanh (< 24h) để giảm churn.
- Hiện tại flow manual mất 3-5 ngày do verify thủ công.
- Target: tự động hoá 80% bước verify bằng eKYC + rule engine.

## 3. Requirements
### 3.1 Product Requirements
- API tạo merchant profile (POST /merchants)
- API upload giấy tờ (PUT /merchants/{id}/documents)
- API check trạng thái onboarding (GET /merchants/{id}/status)
- Webhook notify khi status thay đổi

### 3.2 Technical Requirements
- Rate limit: 100 req/min per merchant
- Timeout: 30s cho eKYC call
- Retry: exponential backoff 3 lần
- Data encryption at rest (AES-256)
- Audit log mọi thao tác

### 3.3 Security
- OAuth2 client credentials flow
- IP whitelist per merchant
- PII fields masked trong log

## 4. Acceptance Criteria
- [ ] Merchant tạo profile thành công trong < 2s (p95)
- [ ] eKYC verify tự động với độ chính xác > 95%
- [ ] Webhook delivery đảm bảo at-least-once
- [ ] Audit log đầy đủ: actor, action, timestamp, resource
- [ ] Rollback được nếu step nào fail (saga pattern)

## 5. Out of Scope
- Mobile SDK
- Batch onboarding
""".strip()


async def _fetch_confluence(url: str) -> Optional[str]:
    """Fetch Confluence page content, returning None on any error."""
    from app.config import settings as _cfg
    if _cfg.use_mock_confluence:
        logger.info("orchestrator: USE_MOCK_CONFLUENCE=true — returning sample PRD content")
        return _MOCK_CONFLUENCE_CONTENT

    from app.integrations.confluence.reader import fetch_page_content
    try:
        content = await fetch_page_content(url)
        logger.info("orchestrator: fetched confluence page (%d chars)", len(content))
        return content
    except ConfluenceUnavailable as exc:
        logger.warning("orchestrator: confluence unavailable — %s", exc)
        raise  # re-raise so caller can build a specific error message
    except Exception as exc:
        logger.error("orchestrator: unexpected confluence error — %s", exc, exc_info=True)
        return None


async def orchestrator_node(state: AgentState) -> Dict[str, Any]:
    client = get_llm_client()
    model = get_model(ModelRole.ORCHESTRATOR)
    messages = state["messages"]

    last_user_text = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_text = m["content"]
            break

    # ── 0. Guardrails: refuse destructive requests early (no LLM/API call) ──
    if _is_confluence_mutate_request(last_user_text):
        logger.info("orchestrator: confluence mutate request refused — %r", last_user_text[:80])
        refusal = (
            "🚫 Xin lỗi, tôi **không được phép sửa/xóa Confluence**. "
            "Quyền truy cập Confluence của Agent là **chỉ đọc (read-only)** — "
            "chỉ dùng để đọc PRD/System Design enrich context.\n\n"
            "Nếu cần chỉnh sửa hay xóa trang Confluence, vui lòng thao tác trực tiếp trên Confluence."
        )
        return {
            **state,
            "messages": [*messages, {"role": "assistant", "content": refusal}],
            "ready_to_generate": False,
            "ready_to_update": False,
        }

    if _is_delete_request(last_user_text):
        logger.info("orchestrator: jira delete request refused — %r", last_user_text[:80])
        refusal = (
            "🚫 Xin lỗi, tôi **không được phép xóa** ticket Jira. "
            "Agent chỉ hỗ trợ: **đọc (Read)**, **tạo mới (Create)** và "
            "**cập nhật có xác nhận (Update)**.\n\n"
            "Nếu cần xóa ticket, vui lòng thao tác trực tiếp trên Jira hoặc liên hệ admin."
        )
        return {
            **state,
            "messages": [*messages, {"role": "assistant", "content": refusal}],
            "ready_to_generate": False,
            "ready_to_update": False,
        }

    # ── 0c. Statistics request → route straight to stats node (read-only) ──
    # Only when not in the middle of a create/update confirmation, to avoid hijacking.
    if (
        _is_stats_request(last_user_text)
        and not state.get("draft_shown")
        and not state.get("update_draft_shown")
    ):
        logger.info("orchestrator: stats request detected — %r", last_user_text[:80])
        return {
            **state,
            "ready_for_stats": True,
            "ready_to_generate": False,
            "ready_to_update": False,
        }

    confluence_url = state.get("confluence_url")
    confluence_data = state.get("confluence_data")
    jira_context = state.get("jira_context")
    conf_fetch_error: Optional[str] = None   # set when Confluence fetch fails this turn
    extra_notice = ""

    # ── 0a. Detect & fetch Jira issue URL ─────────────────────────────────
    jira_key = _extract_jira_issue_key(last_user_text)
    if jira_key:
        fetched_issue = await _fetch_jira_issue(jira_key)
        if fetched_issue:
            jira_context = fetched_issue
            logger.info("orchestrator: jira context loaded for %s", jira_key)
        else:
            logger.warning("orchestrator: could not fetch Jira issue %s", jira_key)

    # ── 0b. Detect & fetch Confluence URL ────────────────────────────────
    # Re-fetch when a NEW url is sent, or when we have a url but no data yet
    # (e.g. a previous fetch failed) — never get stuck after one failed attempt.
    detected_conf = _extract_confluence_url(last_user_text)
    if detected_conf and (detected_conf != confluence_url or confluence_data is None):
        confluence_url = detected_conf
        confluence_data = None
        logger.info("orchestrator: fetching confluence_url=%s", confluence_url)
        try:
            fetched_conf = await _fetch_confluence(confluence_url)
            if fetched_conf is not None:
                confluence_data = fetched_conf
                logger.info("orchestrator: confluence_data loaded (%d chars)", len(fetched_conf))
        except ConfluenceUnavailable as exc:
            conf_fetch_error = (
                f"Fetch Confluence thất bại: {exc}. "
                "Hãy thông báo lý do lỗi cụ thể cho user và đề nghị họ paste nội dung trực tiếp vào chat."
            )

    # ── 0d. Self-assign: resolve Jira username BEFORE calling LLM ────────────
    # Detect "gán cho tôi" / "assign for me" early and look up the real Jira
    # username from the Teams user's email.  Pre-filling the slot here ensures the
    # LLM draft always shows the correct Jira username (e.g. "bachnt"), not a
    # display name that would fail on Jira.
    current_user = state.get("current_user") or {}
    slots = dict(state.get("slots") or {})
    pre_filled_assignee: Optional[str] = None  # injected into system prompt when set

    if _is_self_assign(last_user_text) and current_user:
        email = current_user.get("email", "")
        resolved_username: Optional[str] = None

        if email:
            try:
                from app.config import settings as _settings
                from app.integrations.jira.client import get_jira_client
                jira_client = get_jira_client()
                resolved_username = await jira_client.get_jira_username_by_email(email)
            except Exception as _exc:
                logger.warning("orchestrator: could not resolve Jira username for self-assign — %s", _exc)

        if resolved_username:
            slots["assignee"] = resolved_username
            pre_filled_assignee = resolved_username
            logger.info("orchestrator: self-assign → pre-filled assignee slot = '%s'", resolved_username)
        elif email:
            # Jira not reachable or no match — use email as fallback so draft is still useful
            slots["assignee"] = email
            pre_filled_assignee = email
            logger.info("orchestrator: self-assign fallback → pre-filled assignee slot = '%s'", email)

    # ── 1. Build LLM messages, injecting fetched context if available ──────
    system_content = SYSTEM_PROMPT

    # Inject Teams identity so LLM sees who "tôi" is
    if current_user and (current_user.get("name") or current_user.get("email")):
        name_str = current_user.get("name") or ""
        email_str = current_user.get("email") or ""
        user_line = f"Tên: {name_str}" if name_str else ""
        if email_str:
            user_line += f"  |  Email: {email_str}" if user_line else f"Email: {email_str}"
        system_content += f"\n\n---\n## Người dùng hiện tại (Teams identity)\n{user_line}"

    if pre_filled_assignee:
        system_content += (
            f"\n\n---\n## Slot đã được tự động điền\n"
            f"Assignee: **{pre_filled_assignee}** (Jira username của người dùng hiện tại)\n"
            "→ Dùng giá trị này trực tiếp trong ticket draft. **KHÔNG hỏi lại username hay email.**"
        )

    # ── 0e. Sprint context: fetch real sprint names when relevant ─────────────
    # When the user mentions "sprint" and we know the project, inject actual sprint
    # names so the LLM can present concrete options instead of abstract labels.
    if _needs_sprint_context(last_user_text, slots):
        project_key_for_sprint = (
            slots.get("project_key") or _extract_project_key_from_text(last_user_text) or ""
        )
        if project_key_for_sprint:
            try:
                from app.config import settings as _cfg
                from app.integrations.jira.client import get_jira_client
                if not _cfg.use_mock_jira:
                    sprint_ctx = await get_jira_client().get_sprint_context(project_key_for_sprint)
                    if sprint_ctx:
                        system_content += (
                            f"\n\n---\n## Sprint thực tế của project `{project_key_for_sprint}`\n"
                            f"```\n{sprint_ctx}\n```\n"
                            "→ Dùng tên sprint ở trên khi trả lời user. Gợi ý Active Sprint là sprint "
                            "đang chạy; Next Sprint là Future sprint gần nhất (đánh dấu ← Next Sprint). "
                            "**KHÔNG dùng nhãn chung chung nếu đã có tên thật.**"
                        )
            except Exception as _exc:
                logger.warning("orchestrator: sprint context fetch failed — %s", _exc)

    if jira_context:
        system_content += (
            "\n\n---\n## Nội dung Jira issue đã fetch (dùng để trả lời user)\n\n"
            + jira_context
        )
    if confluence_data:
        system_content += (
            "\n\n---\n## Nội dung Confluence page đã fetch (dùng để trả lời user)\n\n"
            + confluence_data[:4000]
        )
    if conf_fetch_error:
        system_content += "\n\n---\n## Lỗi fetch Confluence\n" + conf_fetch_error

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_content},
                *messages,
            ],
        )
        raw_reply = response.choices[0].message.content or ""
        # Check for real tool-call patterns BEFORE stripping (avoid false positive from .strip())
        had_tool_calls = _has_tool_calls(raw_reply)
        cleaned = _strip_tool_calls(raw_reply)
        if had_tool_calls:
            logger.warning(
                "orchestrator: stripped tool-call XML (%d chars removed)",
                len(raw_reply) - len(cleaned),
            )
            if not cleaned:
                cleaned = "Xin lỗi, đang gặp sự cố xử lý. Vui lòng thử lại."
        reply = cleaned + extra_notice
    except Exception as exc:
        logger.error("Orchestrator LLM failed: %s", exc, exc_info=True)
        reply = "Xin lỗi, đang gặp sự cố kết nối. Vui lòng thử lại."
        return {
            **state,
            "messages": [*messages, {"role": "assistant", "content": reply}],
        }

    updated_messages = [*messages, {"role": "assistant", "content": reply}]

    # ── 2. Detect draft shown (create + update are independent) ────────────
    shows_draft = any(marker in reply for marker in _DRAFT_MARKERS)
    draft_shown_now = state["draft_shown"] or shows_draft

    shows_update_draft = any(marker in reply for marker in _UPDATE_DRAFT_MARKERS)
    update_draft_shown_now = state.get("update_draft_shown", False) or shows_update_draft

    # ── 3. Detect user confirmation (only valid AFTER the matching draft) ──
    confirmed = any(kw in last_user_text.lower().strip() for kw in _CONFIRM_KEYWORDS)
    # UPDATE takes precedence when an update draft is pending, so a "có" after an
    # update diff routes to the updater rather than re-triggering create.
    update_confirmed = state.get("update_draft_shown", False) and confirmed
    create_confirmed = state["draft_shown"] and confirmed and not update_confirmed

    return {
        **state,
        "messages": updated_messages,
        "slots": slots,
        "confluence_url": confluence_url,
        "confluence_data": confluence_data,
        "jira_context": jira_context,
        "draft_shown": draft_shown_now,
        "ready_to_generate": create_confirmed,
        "update_draft_shown": update_draft_shown_now,
        "ready_to_update": update_confirmed,
        "ready_for_stats": False,
    }
