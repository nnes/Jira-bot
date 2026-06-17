"""
Teams user identity extraction and Jira authorization.

Identity sources (in priority order):
  1. TeamsInfo.get_member()  — real MS Teams connection, returns email directly
  2. activity.from_property  — works in both Teams and Emulator
  3. TEAMS_TEST_USER_EMAIL   — .env override for local Emulator testing

Authorization rule (enforced before every UPDATE):
  A user may only update a Jira issue if they are the **reporter** OR **assignee**.
  This implicitly prevents cross-project modifications.
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Minimal user dict shape stored in AgentState["current_user"]
# {"id": str, "name": str, "email": str}
UserIdentity = Dict[str, str]


# ── Identity extraction ────────────────────────────────────────────────────────

async def extract_teams_user(turn_context: Any) -> UserIdentity:
    """Return the Teams user identity from the current activity.

    Tries TeamsInfo (real Teams connection) first, then falls back to
    activity.from_property + TEAMS_TEST_USER_EMAIL for Emulator.
    """
    from app.config import settings

    from_prop = turn_context.activity.from_property
    user: UserIdentity = {
        "id": from_prop.id or "",
        "name": from_prop.name or "",
        "email": "",
    }

    # ── 1. TeamsInfo (real MS Teams) ─────────────────────────────────────────
    try:
        from botbuilder.core.teams import TeamsInfo  # type: ignore
        member = await TeamsInfo.get_member(turn_context, from_prop.id)
        if member and getattr(member, "email", None):
            user["email"] = member.email
            logger.debug("authz: identity from TeamsInfo — %s (%s)", user["name"], user["email"])
            return user
    except Exception:
        pass  # Emulator / not a Teams channel

    # ── 2. Emulator override (TEAMS_TEST_USER_EMAIL) ─────────────────────────
    if settings.teams_test_user_email:
        user["email"] = settings.teams_test_user_email
        logger.debug("authz: identity from TEAMS_TEST_USER_EMAIL — %s", user["email"])
        return user

    # ── 3. Derive from display name + domain ─────────────────────────────────
    #   "Bách. Ngô Tùng"  →  cannot reliably derive
    #   "bachnt" (short) + teams_domain  →  "bachnt@vng.com.vn"
    if settings.teams_domain and from_prop.name:
        name_part = from_prop.name.strip()
        # Only use if name looks like a username (no spaces, no dots except separator)
        if re.match(r"^[a-zA-Z0-9._-]+$", name_part):
            user["email"] = f"{name_part}@{settings.teams_domain}"
            logger.debug("authz: identity derived from name+domain — %s", user["email"])
            return user

    logger.warning(
        "authz: could not determine email for user '%s' (id=%s). "
        "Set TEAMS_TEST_USER_EMAIL in .env for Emulator testing.",
        from_prop.name, from_prop.id,
    )
    return user


# ── Authorization ──────────────────────────────────────────────────────────────

async def assert_can_modify_issue(
    jira_client: Any,
    issue_key: str,
    user_email: str,
) -> None:
    """Verify *user_email* is reporter or assignee of *issue_key*.

    Raises PermissionDeniedError if the check fails.
    Skips silently if *user_email* is empty (identity unavailable — Emulator
    without TEAMS_TEST_USER_EMAIL configured).
    """
    from app.core.errors import PermissionDeniedError

    if not user_email:
        logger.warning(
            "authz: skipping permission check for %s — user identity unknown", issue_key
        )
        return

    issue = await jira_client.get_issue(issue_key)
    fields = issue.get("fields", {})

    reporter: Dict[str, Any] = fields.get("reporter") or {}
    assignee: Dict[str, Any] = fields.get("assignee") or {}
    project: Dict[str, Any] = fields.get("project", {})

    reporter_email = (reporter.get("emailAddress") or "").lower()
    assignee_email = (assignee.get("emailAddress") or "").lower()
    user_lower = user_email.lower()

    logger.debug(
        "authz: check %s — user=%s reporter=%s assignee=%s",
        issue_key, user_lower, reporter_email, assignee_email,
    )

    if user_lower in (reporter_email, assignee_email):
        return  # authorized

    reporter_display = reporter.get("displayName") or reporter_email or "unknown"
    assignee_display = assignee.get("displayName") or assignee_email or "Unassigned"
    project_key = project.get("key", "?")

    raise PermissionDeniedError(
        user_email=user_email,
        issue_key=f"{project_key}/{issue_key}",
        reporter=reporter_display,
        assignee=assignee_display,
    )


def get_user_email(state: Dict[str, Any]) -> str:
    """Convenience: pull email from AgentState['current_user'], empty string if missing."""
    current_user = state.get("current_user") or {}
    return current_user.get("email", "")


async def assert_can_view_stats(
    jira_client: Any,
    project_key: str,
    user_email: str,
) -> None:
    """Verify *user_email* is Project Lead or Administrator of *project_key*.

    Raises StatsPermissionDeniedError if the user is neither.
    Skips silently if *user_email* is empty (identity unavailable).
    """
    from app.core.errors import StatsPermissionDeniedError

    if not user_email:
        logger.warning(
            "authz: skipping stats permission check for '%s' — user identity unknown", project_key
        )
        return

    jira_username = await jira_client.get_jira_username_by_email(user_email)
    if not jira_username:
        logger.warning(
            "authz: stats — Jira user not found for email '%s', denying access to '%s'",
            user_email, project_key,
        )
        raise StatsPermissionDeniedError(user_email=user_email, project_key=project_key)

    lead_and_admins = await jira_client.get_project_lead_and_admins(project_key)
    lead: Optional[str] = lead_and_admins.get("lead")
    admin_names: List[str] = lead_and_admins.get("admin_names", [])

    logger.debug(
        "authz stats: user=%s project=%s lead=%s admins=%s",
        jira_username, project_key, lead, admin_names,
    )

    if jira_username == lead or jira_username in admin_names:
        return  # authorized

    raise StatsPermissionDeniedError(user_email=user_email, project_key=project_key)
