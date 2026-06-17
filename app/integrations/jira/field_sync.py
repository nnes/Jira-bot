"""
Auto-sync Jira custom field IDs from the createmeta API.

Called automatically by the generator node when create_issue returns HTTP 400
with a field-related error. Updates the JiraClient singleton in-memory so the
retry uses the correct field IDs immediately.

Note: this does NOT rewrite source files at runtime — discovered field IDs live
in-memory for the process. Set the correct IDs via env vars (JIRA_*_FIELD) so they
survive restarts; this is production-safe (works on read-only filesystems).
"""
import logging
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# ── Jira createmeta ────────────────────────────────────────────────────────────

async def _fetch_createmeta(project_key: str, http: httpx.AsyncClient) -> dict:
    from app.config import settings
    url = (
        f"{settings.jira_server_url.rstrip('/')}/rest/api/2/issue/createmeta"
        f"?projectKeys={project_key}&expand=projects.issuetypes.fields"
    )
    resp = await http.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"createmeta {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _parse_fields(data: dict) -> Dict[str, Dict]:
    """Return {issue_type: {field_id: {name, required}}}"""
    result: Dict[str, Dict] = {}
    for project in data.get("projects", []):
        for issuetype in project.get("issuetypes", []):
            itype: str = issuetype["name"]
            result[itype] = {
                fid: {
                    "name": info.get("name", fid),
                    "required": info.get("required", False),
                }
                for fid, info in issuetype.get("fields", {}).items()
            }
    return result


def _find_special_fields(
    fields_by_type: Dict[str, Dict],
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Detect Epic Name, Epic Link, Story Points, and Sprint field IDs from createmeta."""
    epic_name_id: Optional[str] = None
    epic_link_id: Optional[str] = None
    story_points_id: Optional[str] = None
    sprint_id: Optional[str] = None

    for fid, info in fields_by_type.get("Epic", {}).items():
        if "epic name" in info["name"].lower():
            epic_name_id = fid

    for itype in ("Story", "Task"):
        for fid, info in fields_by_type.get(itype, {}).items():
            name_lower = info["name"].lower()
            if name_lower in ("epic link", "epic"):
                epic_link_id = fid
            if name_lower in ("story points", "story point estimate", "story_points"):
                story_points_id = fid
            if name_lower == "sprint":
                sprint_id = fid

    return epic_name_id, epic_link_id, story_points_id, sprint_id


# ── Public API ────────────────────────────────────────────────────────────────

async def auto_sync(
    project_key: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Fetch createmeta for *project_key* and update the JiraClient singleton in-memory.

    Returns (epic_name_id, epic_link_id, story_points_id, sprint_id) — any may be
    None if not discovered. Does not persist to disk (production-safe).
    """
    from app.config import settings
    from app.integrations.jira.client import get_jira_client

    logger.info("field_sync: auto_sync triggered for project '%s'", project_key)

    headers: Dict[str, str] = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.jira_user_email:
        http_kwargs: Dict = {"auth": (settings.jira_user_email, settings.jira_api_token)}
    else:
        headers["Authorization"] = f"Bearer {settings.jira_api_token}"
        http_kwargs = {}

    async with httpx.AsyncClient(timeout=20, headers=headers, **http_kwargs) as http:
        try:
            data = await _fetch_createmeta(project_key, http)
        except Exception as exc:
            logger.warning("field_sync: createmeta fetch failed — %s", exc)
            return None, None, None, None

    fields_by_type = _parse_fields(data)
    epic_name_id, epic_link_id, story_points_id, sprint_id = _find_special_fields(fields_by_type)

    if not any((epic_name_id, epic_link_id, story_points_id, sprint_id)):
        logger.warning(
            "field_sync: could not detect any special fields for project %s", project_key
        )
        return None, None, None, None

    # Update JiraClient singleton in-memory so the retry uses correct IDs immediately
    try:
        client = get_jira_client()
        client.update_field_ids(epic_name_id, epic_link_id, story_points_id, sprint_id)
        logger.info(
            "field_sync: JiraClient updated — epic_name=%s, epic_link=%s, story_points=%s, sprint=%s",
            epic_name_id, epic_link_id, story_points_id, sprint_id,
        )
    except Exception as exc:
        logger.warning("field_sync: could not update JiraClient — %s", exc)

    return epic_name_id, epic_link_id, story_points_id, sprint_id
