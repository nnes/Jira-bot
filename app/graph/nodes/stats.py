"""Stats node — aggregate / report Jira metrics via read-only JQL search.

Reached when state["ready_for_stats"] is True (stats intent detected in orchestrator).
Uses the GENERATOR model to extract a query spec, builds a safe JQL, runs a read-only
search, aggregates counts + story points, and formats a report. No writes ever.
"""
import ast
import datetime
import json
import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core.errors import JiraError
from app.graph.nodes.generator import _extract_json
from app.graph.state import AgentState
from app.llm.client import get_llm_client
from app.llm.registry import ModelRole, get_model
from app.prompts.stats import STATS_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


# ── Function-call fallback parser ────────────────────────────────────────────
# Some LLMs (e.g. gemma-4-31b-it) output <FunctionCall>...</FunctionCall> instead
# of raw JSON even when instructed otherwise.  This regex matches the wrapper so
# we can attempt to reconstruct a valid spec from the inner content.
_FC_OUTER_RE = re.compile(
    r"<function_?calls?\b[^>]*>(.*?)</function_?calls?>",
    re.DOTALL | re.IGNORECASE,
)
# Dash variants used as CLI-flag prefixes: ASCII hyphen, en-dash, em-dash
_DASH_RE = r"[-–—]+"


def _parse_function_call_to_spec(raw: str) -> Optional[Dict[str, Any]]:
    """Attempt to build a stats spec from an LLM <FunctionCall> output.

    Handles the case where the model wraps a Python-dict function call instead of
    returning a plain JSON object.  Returns None when the format is unrecognised.
    """
    m = _FC_OUTER_RE.search(raw)
    inner = m.group(1).strip() if m else raw.strip()

    # Parse the Python dict with single-quoted keys/values safely via ast.
    try:
        parsed = ast.literal_eval(inner)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None

    args_str = str(parsed.get("args", ""))
    if not args_str:
        return None

    spec: Dict[str, Any] = {"query_type": "issues"}

    # --projectKey / –projectKey
    mp = re.search(_DASH_RE + r"projectKey\s+[\"']?([A-Z][A-Z0-9_]+)[\"']?", args_str)
    if mp:
        spec["project_key"] = mp.group(1)

    # --search / –search  (contains a JQL string)
    ms = re.search(_DASH_RE + r'search\s+["\']([^"\']+)["\']', args_str)
    jql = ms.group(1) if ms else ""

    if jql:
        # assignee
        ma = re.search(r"assignee\s*=\s*[\"']?(\w+)[\"']?", jql, re.IGNORECASE)
        if ma:
            spec["assignee"] = ma.group(1)

        # completion / status
        if re.search(r"status(?:Category)?\s*[=\s]+[\"']?done[\"']?", jql, re.IGNORECASE):
            spec["completed_only"] = True

        # resolved date range
        mdf = re.search(r"resolved\s*>=\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?", jql, re.IGNORECASE)
        if mdf:
            spec["date_from"] = mdf.group(1)
            spec["date_field"] = "resolved"
        mdt = re.search(r"resolved\s*<=\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?", jql, re.IGNORECASE)
        if mdt:
            spec["date_to"] = mdt.group(1)

        # created date range (fallback when no resolved)
        if not spec.get("date_field"):
            mcdf = re.search(r"created\s*>=\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?", jql, re.IGNORECASE)
            if mcdf:
                spec["date_from"] = mcdf.group(1)
                spec["date_field"] = "created"
            mcdt = re.search(r"created\s*<=\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?", jql, re.IGNORECASE)
            if mcdt:
                spec["date_to"] = mcdt.group(1)

        # issue types
        mtype_in = re.search(r"issuetype\s+in\s*\(([^)]+)\)", jql, re.IGNORECASE)
        if mtype_in:
            spec["issue_types"] = [t.strip().strip("\"'") for t in mtype_in.group(1).split(",") if t.strip()]
        else:
            mtype_eq = re.search(r"issuetype\s*=\s*[\"']?(\w+)[\"']?", jql, re.IGNORECASE)
            if mtype_eq:
                spec["issue_types"] = [mtype_eq.group(1)]

    return spec if len(spec) > 1 else None  # at least one meaningful field beyond query_type


# ── JQL builder (pure, testable) ──────────────────────────────────────────────

def _q(value: str) -> str:
    """Quote a JQL string value, stripping embedded double-quotes for safety."""
    return '"' + str(value).replace('"', "") + '"'


def build_stats_jql(spec: Dict[str, Any]) -> str:
    """Build a read-only JQL string from a stats query spec. Returns '' if empty."""
    clauses: List[str] = []

    if spec.get("project_key"):
        clauses.append(f"project = {_q(spec['project_key'])}")
    if spec.get("assignee"):
        clauses.append(f"assignee = {_q(spec['assignee'])}")

    issue_types = spec.get("issue_types") or []
    if issue_types:
        joined = ", ".join(_q(t) for t in issue_types)
        clauses.append(f"issuetype in ({joined})")

    # Sprint filter — use concrete ID when available (resolved from Agile API before
    # this call); fall back to JQL functions only when no ID could be resolved.
    # Concrete IDs are more reliable on Jira Server where openSprints() can return
    # incorrect results depending on board configuration.
    sprint_id = spec.get("sprint_id")
    sprint = (spec.get("sprint") or "").strip().lower()
    if sprint_id is not None:
        clauses.append(f"sprint = {int(sprint_id)}")
    elif sprint == "active":
        clauses.append("sprint in openSprints()")
    elif sprint == "next":
        clauses.append("sprint in futureSprints()")
    elif sprint:
        # Explicit sprint name provided by user (e.g. "PCF-BANK 26.07.A")
        clauses.append(f"sprint = {_q(spec['sprint'])}")

    statuses = spec.get("statuses") or []
    if spec.get("completed_only"):
        clauses.append("statusCategory = Done")
    elif statuses:
        joined = ", ".join(_q(s) for s in statuses)
        clauses.append(f"status in ({joined})")

    # Date range — default field to 'resolved' when filtering completed work
    date_field = spec.get("date_field")
    if not date_field and spec.get("completed_only"):
        date_field = "resolved"
    if date_field and spec.get("date_from"):
        clauses.append(f"{date_field} >= {_q(spec['date_from'])}")
    if date_field and spec.get("date_to"):
        clauses.append(f"{date_field} <= {_q(spec['date_to'])}")

    return " AND ".join(clauses)


# ── Aggregation (pure, testable) ──────────────────────────────────────────────

def aggregate_issues(issues: List[Dict[str, Any]], story_points_field: str) -> Dict[str, Any]:
    """Aggregate counts by type/status and sum story points from a list of issues."""
    by_type: Counter = Counter()
    by_status: Counter = Counter()
    total_points = 0.0
    for issue in issues:
        f = issue.get("fields", {}) or {}
        itype = (f.get("issuetype") or {}).get("name")
        if itype:
            by_type[itype] += 1
        status = (f.get("status") or {}).get("name")
        if status:
            by_status[status] += 1
        pts = f.get(story_points_field)
        if isinstance(pts, (int, float)):
            total_points += pts
    return {
        "by_type": dict(by_type),
        "by_status": dict(by_status),
        "total_points": int(total_points) if total_points.is_integer() else total_points,
    }


def _format_report(
    total: int,
    agg: Dict[str, Any],
    spec: Dict[str, Any],
    truncated: bool,
    sampled: int,
    assignee_display: Optional[str] = None,
) -> str:
    lines = ["📊 **Thống kê Jira**", ""]

    scope: List[str] = []
    if spec.get("project_key"):
        scope.append(f"Project: `{spec['project_key']}`")
    if spec.get("assignee"):
        # Show "Display Name (username)" if real name was resolved, else just username
        if assignee_display and assignee_display != spec["assignee"]:
            scope.append(f"Assignee: {assignee_display} (`{spec['assignee']}`)")
        else:
            scope.append(f"Assignee: `{spec['assignee']}`")
    sprint_val = (spec.get("sprint") or "").strip().lower()
    sprint_name = spec.get("sprint_name", "")
    if sprint_name:
        scope.append(f"Sprint: `{sprint_name}`")
    elif sprint_val == "active":
        scope.append("Sprint: Active (sprint đang chạy)")
    elif sprint_val == "next":
        scope.append("Sprint: Next (sprint kế tiếp)")
    elif sprint_val:
        scope.append(f"Sprint: `{spec['sprint']}`")
    if spec.get("completed_only"):
        scope.append("Trạng thái: đã hoàn thành (Done)")
    if spec.get("date_from") or spec.get("date_to"):
        scope.append(f"Thời gian: {spec.get('date_from') or '…'} → {spec.get('date_to') or '…'}")
    if scope:
        lines.append("· " + " · ".join(scope))
        lines.append("")

    lines.append(f"**Tổng số issue:** {total}")
    lines.append(f"**Tổng Story Points:** {agg['total_points']}")

    if agg["by_type"]:
        lines.append("\n**Theo loại:**")
        for t, c in sorted(agg["by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"- {t}: {c}")
    if agg["by_status"]:
        lines.append("\n**Theo trạng thái:**")
        for s, c in sorted(agg["by_status"].items(), key=lambda x: -x[1]):
            lines.append(f"- {s}: {c}")

    if truncated:
        lines.append(
            f"\n_⚠️ Story Points & phân loại chỉ tính trên {sampled} issue đầu tiên; "
            "tổng số issue ở trên là chính xác._"
        )
    return "\n".join(lines)


def _format_members_report(project_key: str, members: List[Dict[str, Any]]) -> str:
    if not members:
        return (
            f"⚠️ Không tìm thấy thành viên nào trong project `{project_key}`.\n"
            "Có thể bot chưa có quyền xem project roles, hoặc project chưa có issue nào được assign."
        )

    lines = [f"👥 **Thành viên project `{project_key}`**", ""]

    # Group by role
    by_role: Dict[str, List[Dict[str, Any]]] = {}
    for m in members:
        role = m.get("role") or "Unknown"
        by_role.setdefault(role, []).append(m)

    for role, role_members in sorted(by_role.items()):
        lines.append(f"**{role}** ({len(role_members)} người):")
        for m in sorted(role_members, key=lambda x: x.get("display_name", "").lower()):
            display = m.get("display_name") or m.get("name") or "?"
            username = m.get("name") or ""
            mtype = m.get("type", "user")
            if mtype == "group":
                lines.append(f"  - 👥 `{username}` _(group)_")
            else:
                suffix = f" (`{username}`)" if username and username != display else ""
                lines.append(f"  - {display}{suffix}")
        lines.append("")

    lines.append(f"_Tổng: **{len(members)} thành viên** ({len(by_role)} role)_")
    return "\n".join(lines)


def _format_lead_report(project_key: str, info: Dict[str, Any]) -> str:
    lead = info.get("lead")
    admins: List[str] = info.get("admin_names") or []

    lines = [f"🏷️ **Project Lead & Admin — `{project_key}`**", ""]
    if lead:
        lines.append(f"**Project Lead:** `{lead}`")
    else:
        lines.append("**Project Lead:** _(không tìm thấy hoặc chưa được cấu hình)_")

    if admins:
        lines.append(f"\n**Administrators** ({len(admins)} người):")
        for name in sorted(admins):
            lines.append(f"  - `{name}`")
    else:
        lines.append("\n**Administrators:** _(không có hoặc bot chưa có quyền xem)_")

    return "\n".join(lines)


# ── Node ──────────────────────────────────────────────────────────────────────

async def stats_node(state: AgentState) -> Dict[str, Any]:
    client = get_llm_client()
    model = get_model(ModelRole.GENERATOR)
    messages = state["messages"]
    reset = {"ready_for_stats": False}

    last_user_text = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_text = m["content"]
            break

    today = datetime.date.today().isoformat()
    user_prompt = (
        f"HÔM NAY: {today}\n\n"
        f"YÊU CẦU THỐNG KÊ:\n{last_user_text}\n\n"
        "Trích xuất query spec. Trả về JSON only."
    )

    # ── 1. Extract the query spec ─────────────────────────────────────────────
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": STATS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw_llm = response.choices[0].message.content or "{}"
        spec = _extract_json(raw_llm)

        if not spec:
            # LLM may have output <FunctionCall> format instead of JSON — attempt recovery
            if _FC_OUTER_RE.search(raw_llm) or "FunctionCall" in raw_llm:
                logger.warning(
                    "stats_node: LLM returned function-call format instead of JSON spec "
                    "(first 300 chars): %s", raw_llm[:300]
                )
                spec = _parse_function_call_to_spec(raw_llm) or {}
                if spec:
                    logger.info("stats_node: recovered spec from function-call fallback: %s", spec)
    except Exception as exc:
        logger.error("Stats LLM failed: %s", exc, exc_info=True)
        reply = "Xin lỗi, không thể xử lý yêu cầu thống kê lúc này. Vui lòng thử lại."
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    project_key = spec.get("project_key") or ""
    query_type = (spec.get("query_type") or "issues").lower()

    # ── Permission check — Project Lead / Admin only ──────────────────────────
    # Only enforced in real Jira mode (mock mode has no role data to check against).
    if project_key and not settings.use_mock_jira:
        from app.core.authz import assert_can_view_stats, get_user_email
        from app.core.errors import StatsPermissionDeniedError
        from app.integrations.jira.client import get_jira_client as _get_jira
        _jira_auth = _get_jira()
        _user_email = get_user_email(state)
        try:
            await assert_can_view_stats(_jira_auth, project_key, _user_email)
        except StatsPermissionDeniedError as exc:
            logger.warning("stats: access denied for user '%s' on project '%s'", _user_email, project_key)
            reply = (
                f"🔒 **Không có quyền truy cập**\n\n"
                f"Tính năng thống kê chỉ dành cho **Project Lead** hoặc **Administrators** "
                f"của project `{project_key}`.\n\n"
                "_Nếu bạn cần xem thống kê, vui lòng liên hệ Project Lead hoặc Admin của project._"
            )
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    # ── 2a. Lead / Admin query ────────────────────────────────────────────────
    if query_type == "lead":
        if not project_key:
            reply = "⚠️ Bạn cho mình biết **mã project** (ví dụ: EWL, PCFBANK) để mình tìm Project Lead nhé."
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

        if settings.use_mock_jira:
            reply = f"📋 (mock mode) Sẽ gọi GET /rest/api/2/project/{project_key} để lấy Project Lead & Admins."
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

        from app.integrations.jira.client import get_jira_client
        jira = get_jira_client()
        try:
            info = await jira.get_project_lead_and_admins(project_key)
            reply = _format_lead_report(project_key, info)
        except Exception as exc:
            logger.error("Stats lead query failed: %s", exc, exc_info=True)
            reply = f"⚠️ Không thể lấy thông tin Project Lead: {exc}"
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    # ── 2b. Members query ─────────────────────────────────────────────────────
    if query_type == "members":
        if not project_key:
            reply = "⚠️ Bạn cho mình biết **mã project** (ví dụ: EWL, PCFBANK) để mình lấy danh sách thành viên nhé."
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

        if settings.use_mock_jira:
            reply = (
                f"📋 (mock mode) Sẽ gọi Jira project roles API cho project `{project_key}`\n"
                "để lấy danh sách thành viên theo role."
            )
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

        from app.integrations.jira.client import get_jira_client
        jira = get_jira_client()
        try:
            members = await jira.get_project_members(project_key)
            reply = _format_members_report(project_key, members)
        except Exception as exc:
            logger.error("Stats members failed: %s", exc, exc_info=True)
            reply = f"⚠️ Không thể lấy danh sách thành viên: {exc}"
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    # ── 2b. Issues / JQL query ────────────────────────────────────────────────

    # Initialise the Jira client early (needed for both sprint resolution and search).
    # Sprint resolution must happen BEFORE build_stats_jql so we can inject a concrete
    # sprint ID — `sprint in openSprints()` JQL function is unreliable on Jira Server
    # and can return 0 results even when active-sprint issues exist.
    jira = None
    if not settings.use_mock_jira:
        from app.integrations.jira.client import get_jira_client
        jira = get_jira_client()

        sprint_val = (spec.get("sprint") or "").strip().lower()
        if sprint_val in ("active", "next") and project_key:
            sprint_obj = await jira.resolve_sprint_object(project_key, sprint_val)
            if sprint_obj:
                spec = {
                    **spec,
                    "sprint_id": sprint_obj.get("id"),
                    "sprint_name": sprint_obj.get("name", ""),
                }
                logger.info(
                    "stats: sprint resolved → '%s' (id=%s)",
                    sprint_obj.get("name"), sprint_obj.get("id"),
                )
            else:
                logger.warning(
                    "stats: sprint '%s' not found for project %s — falling back to JQL function",
                    sprint_val, project_key,
                )

    jql = build_stats_jql(spec)
    if not jql:
        reply = (
            "⚠️ Tôi chưa rõ phạm vi thống kê. Bạn cho biết **project** (hoặc **assignee**), "
            "loại issue, trạng thái và khoảng thời gian giúp tôi nhé."
        )
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    logger.info("stats: JQL = %s", jql)

    if settings.use_mock_jira:
        reply = f"📊 (mock mode) JQL sẽ chạy:\n```\n{jql}\n```"
        return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    # Resolve assignee display name from Jira — never hallucinate.
    # If the username does not exist, abort immediately instead of running JQL
    # against a non-existent user and returning misleading empty results.
    assignee_display: Optional[str] = None
    if spec.get("assignee"):
        user_data = await jira.get_user_by_username(spec["assignee"])
        if user_data:
            assignee_display = user_data.get("displayName") or spec["assignee"]
        else:
            reply = (
                f"⚠️ Không tìm thấy username **`{spec['assignee']}`** trong hệ thống Jira.\n"
                "Vui lòng kiểm tra lại username và thử lại."
            )
            return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}

    try:
        result = await jira.search_issues(
            jql,
            fields=["issuetype", "status", jira._story_points_field],
        )
        agg = aggregate_issues(result["issues"], jira._story_points_field)
        reply = _format_report(
            result["total"], agg, spec, result["truncated"], len(result["issues"]),
            assignee_display=assignee_display,
        )
    except JiraError as exc:
        logger.error("Stats search failed: %s", exc, exc_info=True)
        reply = f"⚠️ Không thể chạy thống kê: {exc}"
    except Exception as exc:
        logger.error("Unexpected stats error: %s", exc, exc_info=True)
        reply = "⚠️ Đã xảy ra lỗi không mong đợi khi thống kê. Vui lòng thử lại."

    return {**state, "messages": [*messages, {"role": "assistant", "content": reply}], **reset}
