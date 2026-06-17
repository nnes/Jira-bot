import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.core.errors import JiraError, JiraFieldError, JiraIssueNotFound
from app.core.retry import with_retry
from app.integrations.jira.guards import require_update_confirmation

logger = logging.getLogger(__name__)

# Sprint-label synonyms (lowercased) for the active / next-future sprint.
_ACTIVE_SPRINT_SYN = frozenset({
    "active sprint", "active", "current sprint", "current", "sprint hiện tại",
})
_NEXT_SPRINT_SYN = frozenset({
    "next sprint", "next", "future", "sprint kế tiếp", "sprint tiếp theo", "sprint tới",
})

# Sprint name convention used across projects: "XXXX YY.MM.A/B/C" (e.g. "PCF-BANK 26.06.B")
_SPRINT_NAME_RE = re.compile(r"(\d{2})\.(\d{1,2})\.([A-Za-z])")


def _parse_sprint_name_key(name: str):
    """Return a chronological sort key (yy, mm, letter_ord) from a sprint name, or None.

    Parses the 'YY.MM.A/B/C' portion so future sprints sort earliest-first regardless
    of the order the Agile API returns them.
    """
    m = _SPRINT_NAME_RE.search(name or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), ord(m.group(3).upper()))


def _sprint_sort_key(sprint: Dict[str, Any]):
    """Sort key for ordering future sprints: parsed name first, unparseable last."""
    key = _parse_sprint_name_key(sprint.get("name", ""))
    return (0, key) if key is not None else (1, (9999, 99, 999))


def _select_sprint(sprints: List[Dict[str, Any]], label: str) -> Optional[Dict[str, Any]]:
    """Choose a sprint from *sprints* given a human *label*.

    - 'Active Sprint' / synonyms  → the active sprint
    - 'Next Sprint' / synonyms     → earliest future sprint (by 'YY.MM.A/B/C' order)
    - an explicit name             → exact (normalised) match, else substring match
    """
    norm = re.sub(r"\s+", " ", (label or "").strip().lower())
    if not norm:
        return None

    if norm in _ACTIVE_SPRINT_SYN:
        actives = [s for s in sprints if s.get("state") == "active"]
        return actives[0] if actives else None

    if norm in _NEXT_SPRINT_SYN:
        futures = [s for s in sprints if s.get("state") == "future"]
        return sorted(futures, key=_sprint_sort_key)[0] if futures else None

    # Explicit sprint name (e.g. "PCF-BANK 26.07.A")
    for s in sprints:
        if re.sub(r"\s+", " ", (s.get("name") or "").strip().lower()) == norm:
            return s
    for s in sprints:
        if norm in (s.get("name") or "").lower():
            return s
    return None

_PRIORITY_MAP: Dict[str, str] = {
    "P1 (Critical)": "Critical",
    "P2 (High)": "High",
    "P3 (Medium)": "Medium",
    "P4 (Low)": "Low",
}

_401_HINT = (
    "Authentication failed (401). Kiểm tra:\n"
    "  1. JIRA_USER_EMAIL + JIRA_API_TOKEN trong .env có đúng không\n"
    "  2. Jira Server dùng PAT → để JIRA_USER_EMAIL trống, đặt JIRA_API_TOKEN=<PAT>\n"
    "  3. Jira Cloud → JIRA_USER_EMAIL=email, JIRA_API_TOKEN=API token (không phải password)\n"
    "  4. Gọi GET /api/jira/check để test auth trực tiếp"
)


class JiraClient:
    """Jira Server REST API v2 client — Read + Create ONLY.

    Auth modes:
    - JIRA_USER_EMAIL set   → Basic auth  (email / username + api_token)
    - JIRA_USER_EMAIL blank → Bearer PAT  (api_token as Personal Access Token)

    Security contract:
    - No delete / destructive methods are exposed.
    - update_issue() always raises UpdateConfirmationRequired.
    """

    def __init__(
        self,
        server_url: str,
        user_email: str,
        api_token: str,
        epic_link_field: str = "customfield_10014",
        epic_name_field: str = "customfield_10011",
        story_points_field: str = "customfield_10016",
        sprint_field: str = "customfield_10007",
        task_category_field: str = "customfield_12404",
        host_header: str = "",
    ) -> None:
        self._server = server_url.rstrip("/")
        self._base = self._server + "/rest/api/2"
        self._agile_base = self._server + "/rest/agile/1.0"
        self._epic_link_field = epic_link_field
        self._epic_name_field = epic_name_field
        self._story_points_field = story_points_field
        self._sprint_field = sprint_field
        self._task_category_field = task_category_field

        # Build headers + auth at client level so every request inherits them
        base_headers: Dict[str, str] = {
            "Accept": "application/json",        # force JSON error responses from Jira
            "Content-Type": "application/json",
        }

        if host_header:
            base_headers["Host"] = host_header
            logger.info(
                "JiraClient: Host header override → %s (HTTP/1.1, SSL verify disabled)", host_header
            )

        verify_ssl = not bool(host_header)

        if user_email:
            logger.debug("JiraClient: Basic auth (%s)", user_email)
            self._http = httpx.AsyncClient(
                timeout=30.0,
                headers=base_headers,
                auth=(user_email, api_token),
                verify=verify_ssl,
            )
        else:
            logger.debug("JiraClient: Bearer PAT auth")
            self._http = httpx.AsyncClient(
                timeout=30.0,
                headers={**base_headers, "Authorization": f"Bearer {api_token}"},
                verify=verify_ssl,
            )

    # ── Diagnostics ─────────────────────────────────────────────────────────

    async def check_auth(self) -> Dict[str, Any]:
        """Test connectivity and authentication by calling GET /rest/api/2/myself.

        Raises JiraError with actionable hint on 401.
        """
        url = f"{self._base}/myself"
        resp = await self._http.get(url)
        if resp.status_code == 401:
            raise JiraError(_401_HINT)
        if resp.status_code != 200:
            raise JiraError(
                f"check_auth → HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def get_jira_username_by_email(self, email: str) -> Optional[str]:
        """Look up the Jira Server username for an email address.

        Uses GET /rest/api/2/user/search?username={email}.
        Results (including None) are cached in-process to avoid repeated calls.
        Never raises — all errors are logged and return None.
        """
        key = email.lower()
        if key in _jira_username_cache:
            return _jira_username_cache[key]
        try:
            resp = await self._http.get(
                f"{self._base}/user/search",
                params={"username": email, "maxResults": 1},
            )
            if resp.status_code == 200:
                results = resp.json()
                if results:
                    username: Optional[str] = results[0].get("name")
                    _jira_username_cache[key] = username
                    logger.debug("get_jira_username_by_email: %s → %s", email, username)
                    return username
            logger.warning(
                "get_jira_username_by_email: no user found for '%s' (HTTP %s)",
                email, resp.status_code,
            )
        except Exception as exc:
            logger.warning("get_jira_username_by_email: lookup failed for %s — %s", email, exc)
        _jira_username_cache[key] = None
        return None

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Fetch real user profile from Jira by username.

        Calls GET /rest/api/2/user?username={username}.
        Returns the raw Jira user dict (keys: name, displayName, emailAddress, active, ...)
        or None if not found / any error. Never raises. Result is cached.
        """
        key = f"__profile__{username.lower()}"
        if key in _jira_username_cache:
            return _jira_username_cache[key]  # type: ignore[return-value]
        try:
            resp = await self._http.get(
                f"{self._base}/user",
                params={"username": username},
            )
            if resp.status_code == 200:
                data = resp.json()
                _jira_username_cache[key] = data  # type: ignore[assignment]
                logger.debug("get_user_by_username: %s → %s", username, data.get("displayName"))
                return data
            logger.warning(
                "get_user_by_username: user '%s' not found (HTTP %s)", username, resp.status_code
            )
        except Exception as exc:
            logger.warning("get_user_by_username: lookup failed for '%s' — %s", username, exc)
        _jira_username_cache[key] = None  # type: ignore[assignment]
        return None

    async def get_bot_username(self) -> Optional[str]:
        """Return the Jira username of the authenticated bot (from /myself).

        Cached after the first successful call. Never raises.
        """
        global _bot_username
        if _bot_username is not None:
            return _bot_username
        try:
            resp = await self._http.get(f"{self._base}/myself")
            if resp.status_code == 200:
                _bot_username = resp.json().get("name")
                logger.debug("get_bot_username: bot username = %s", _bot_username)
                return _bot_username
            logger.warning("get_bot_username: HTTP %s", resp.status_code)
        except Exception as exc:
            logger.warning("get_bot_username: failed — %s", exc)
        return None

    # ── Read ────────────────────────────────────────────────────────────────

    @with_retry(max_attempts=3, backoff_base=1.0, exceptions=(httpx.HTTPError,))
    async def get_issue(self, issue_key: str) -> Dict[str, Any]:
        """Fetch an issue. Raises JiraIssueNotFound if the key does not exist."""
        url = f"{self._base}/issue/{issue_key}"
        resp = await self._http.get(url)
        if resp.status_code == 401:
            raise JiraError(_401_HINT)
        if resp.status_code == 404:
            raise JiraIssueNotFound(issue_key)
        if resp.status_code != 200:
            raise JiraError(
                f"get_issue({issue_key}) → HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    @with_retry(max_attempts=3, backoff_base=1.0, exceptions=(httpx.HTTPError,))
    async def search_issues(
        self,
        jql: str,
        fields: Optional[list] = None,
        max_results: int = 500,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """Run a read-only JQL search via /rest/api/2/search.

        Paginates up to *max_results* issues. Returns:
            {"total": int, "issues": [...], "truncated": bool}
        where total is Jira's authoritative match count and issues holds the
        fetched page(s) (capped at max_results — truncated=True if more exist).
        """
        url = f"{self._base}/search"
        collected: list = []
        total = 0
        start_at = 0
        while start_at < max_results:
            payload: Dict[str, Any] = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": min(page_size, max_results - start_at),
            }
            if fields is not None:
                payload["fields"] = fields
            resp = await self._http.post(url, json=payload)
            if resp.status_code == 401:
                raise JiraError(_401_HINT)
            if resp.status_code == 400:
                raise JiraError(f"search_issues bad JQL → {resp.text[:300]}")
            if resp.status_code != 200:
                raise JiraError(
                    f"search_issues → HTTP {resp.status_code}: {resp.text[:300]}"
                )
            body = resp.json()
            total = body.get("total", 0)
            batch = body.get("issues", [])
            collected.extend(batch)
            if not batch or start_at + len(batch) >= total:
                break
            start_at += len(batch)
        return {
            "total": total,
            "issues": collected,
            "truncated": total > len(collected),
        }

    # ── Project members (read-only) ─────────────────────────────────────────

    async def get_project_members(self, project_key: str) -> List[Dict[str, Any]]:
        """Return members of a project grouped by role.

        Uses GET /rest/api/2/project/{key}/role to list role URLs, then fetches
        each role to retrieve actors (users/groups).

        Returns a list of dicts:
            [{"role": "Developer", "name": "bachnt", "display_name": "Bach Ngo Tung",
              "email": "bachnt@vng.com.vn", "type": "atlassian-user-role-actor"}, ...]

        Never raises — returns [] on any error (role API may require project-admin).
        Falls back to assignee aggregation from recent issues when roles API returns
        nothing useful (e.g. bot lacks project-admin permission).
        """
        members: List[Dict[str, Any]] = []

        # Step 1: get the role map  { "Developer": "<url>", ... }
        roles_url = f"{self._base}/project/{project_key}/role"
        try:
            resp = await self._http.get(roles_url)
        except Exception as exc:
            logger.warning("get_project_members: roles request failed — %s", exc)
            return await self._members_from_assignees(project_key)

        if resp.status_code == 403:
            logger.warning(
                "get_project_members: 403 on roles API — bot may lack project-admin. "
                "Falling back to assignee aggregation."
            )
            return await self._members_from_assignees(project_key)

        if resp.status_code != 200:
            logger.warning(
                "get_project_members: roles → HTTP %s", resp.status_code
            )
            return await self._members_from_assignees(project_key)

        role_map: Dict[str, str] = resp.json()  # {"Developer": "http://.../role/10001"}

        # Step 2: fetch each role URL and collect actors
        for role_name, role_url in role_map.items():
            try:
                r = await self._http.get(role_url)
                if r.status_code != 200:
                    continue
                for actor in r.json().get("actors", []):
                    actor_type = actor.get("type", "")
                    if actor_type == "atlassian-user-role-actor":
                        user = actor.get("actorUser") or {}
                        members.append({
                            "role": role_name,
                            "name": actor.get("name", ""),
                            "display_name": actor.get("displayName", actor.get("name", "")),
                            "email": user.get("accountId", ""),
                            "type": "user",
                        })
                    elif actor_type == "atlassian-group-role-actor":
                        members.append({
                            "role": role_name,
                            "name": actor.get("name", ""),
                            "display_name": actor.get("displayName", actor.get("name", "")),
                            "email": "",
                            "type": "group",
                        })
            except Exception as exc:
                logger.warning("get_project_members: failed fetching role '%s' — %s", role_name, exc)

        if not members:
            # Roles returned but no actors visible — fall back to assignee aggregation
            logger.info(
                "get_project_members: roles API returned no actors for '%s' — "
                "falling back to assignee aggregation",
                project_key,
            )
            return await self._members_from_assignees(project_key)

        return members

    async def get_project_lead_and_admins(self, project_key: str) -> Dict[str, Any]:
        """Return project lead username and administrator usernames for *project_key*.

        Fetches project lead from GET /rest/api/2/project/{key} and usernames from
        project roles whose name contains 'admin' (case-insensitive).

        Returns: {"lead": str | None, "admin_names": List[str]}
        Never raises — logs and returns empty/None on any error.
        """
        result: Dict[str, Any] = {"lead": None, "admin_names": []}

        # 1. Project lead (from project config)
        try:
            resp = await self._http.get(f"{self._base}/project/{project_key}")
            if resp.status_code == 200:
                lead_obj = resp.json().get("lead") or {}
                result["lead"] = lead_obj.get("name") or None
        except Exception as exc:
            logger.warning(
                "get_project_lead_and_admins: project fetch failed for %s — %s", project_key, exc
            )

        # 2. Administrators role (any role whose name contains 'admin')
        try:
            resp = await self._http.get(f"{self._base}/project/{project_key}/role")
            if resp.status_code == 200:
                for role_name, role_url in resp.json().items():
                    if "admin" not in role_name.lower():
                        continue
                    try:
                        r = await self._http.get(role_url)
                        if r.status_code != 200:
                            continue
                        for actor in r.json().get("actors", []):
                            if actor.get("type") == "atlassian-user-role-actor":
                                name = actor.get("name", "")
                                if name:
                                    result["admin_names"].append(name)
                    except Exception as exc:
                        logger.warning(
                            "get_project_lead_and_admins: role '%s' fetch failed — %s",
                            role_name, exc,
                        )
        except Exception as exc:
            logger.warning(
                "get_project_lead_and_admins: roles fetch failed for %s — %s", project_key, exc
            )

        logger.debug(
            "get_project_lead_and_admins(%s): lead=%s admins=%s",
            project_key, result["lead"], result["admin_names"],
        )
        return result

    async def _members_from_assignees(self, project_key: str) -> List[Dict[str, Any]]:
        """Fallback: aggregate unique assignees from recent issues of the project."""
        try:
            result = await self.search_issues(
                f'project = "{project_key}" AND assignee is not EMPTY',
                fields=["assignee"],
                max_results=500,
            )
        except JiraError as exc:
            logger.warning("_members_from_assignees: search failed — %s", exc)
            return []

        seen: Dict[str, Dict[str, Any]] = {}
        for issue in result.get("issues", []):
            assignee = (issue.get("fields") or {}).get("assignee") or {}
            name = assignee.get("name", "")
            if name and name not in seen:
                seen[name] = {
                    "role": "Assignee (từ issues)",
                    "name": name,
                    "display_name": assignee.get("displayName", name),
                    "email": assignee.get("emailAddress", ""),
                    "type": "user",
                }
        return list(seen.values())

    # ── Agile API (read-only — sprint resolution) ────────────────────────────

    @with_retry(max_attempts=2, backoff_base=1.0, exceptions=(httpx.HTTPError,))
    async def get_boards_for_project(self, project_key: str) -> List[Dict[str, Any]]:
        """Return all Agile boards for *project_key* (read-only). Empty list if none."""
        url = f"{self._agile_base}/board"
        try:
            resp = await self._http.get(url, params={"projectKeyOrId": project_key})
        except httpx.HTTPError as exc:
            logger.warning("get_boards_for_project(%s) request error: %s", project_key, exc)
            raise
        if resp.status_code != 200:
            logger.warning(
                "get_boards_for_project(%s) → HTTP %s: %s",
                project_key, resp.status_code, resp.text[:200],
            )
            return []
        return resp.json().get("values", [])

    async def get_board_id_for_project(self, project_key: str) -> Optional[int]:
        """Return the first Agile board id for *project_key*, or None."""
        boards = await self.get_boards_for_project(project_key)
        return int(boards[0]["id"]) if boards else None

    @with_retry(max_attempts=3, backoff_base=1.0, exceptions=(httpx.HTTPError,))
    async def move_issue_to_sprint(self, sprint_id: int, issue_key: str) -> None:
        """Move an issue into a sprint via the Agile API.

        This is the correct way to (re)assign a sprint — the Sprint custom field is
        usually NOT on the edit/create screen, so setting it via the issue-field API
        fails with 'cannot be set ... not on the appropriate screen'.
        """
        url = f"{self._agile_base}/sprint/{sprint_id}/issue"
        resp = await self._http.post(url, json={"issues": [issue_key]})
        if resp.status_code == 401:
            raise JiraError(_401_HINT)
        if resp.status_code not in (200, 204):
            raise JiraError(
                f"move_issue_to_sprint({issue_key}→{sprint_id}) → HTTP {resp.status_code}: {resp.text[:300]}"
            )

    async def get_board_sprints(self, board_id: int) -> List[Dict[str, Any]]:
        """Fetch all active + future sprints on *board_id* (paginated). Read-only."""
        url = f"{self._agile_base}/board/{board_id}/sprint"
        sprints: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            resp = await self._http.get(
                url, params={"state": "active,future", "startAt": start_at, "maxResults": 50}
            )
            if resp.status_code != 200:
                logger.warning("get_board_sprints: board %s → HTTP %s", board_id, resp.status_code)
                break
            body = resp.json()
            batch = body.get("values", [])
            sprints.extend(batch)
            if body.get("isLast", True) or not batch:
                break
            start_at += len(batch)
        return sprints

    async def _find_sprint(
        self, project_key: str, sprint_label: str
    ) -> Optional[Dict[str, Any]]:
        """Shared core for sprint resolution — returns the full sprint dict or None."""
        if not sprint_label:
            return None
        try:
            boards = await self.get_boards_for_project(project_key)
            if not boards:
                logger.info("_find_sprint: no board for project %s", project_key)
                return None
            boards.sort(key=lambda b: 0 if (b.get("type") == "scrum") else 1)

            seen_ids: set = set()
            aggregated: List[Dict[str, Any]] = []
            for board in boards:
                board_id = board.get("id")
                if board_id is None:
                    continue
                for s in await self.get_board_sprints(board_id):
                    if s.get("id") not in seen_ids:
                        seen_ids.add(s.get("id"))
                        aggregated.append(s)
                chosen = _select_sprint(aggregated, sprint_label)
                if chosen:
                    logger.info(
                        "_find_sprint: %r → '%s' (id=%s, board=%s)",
                        sprint_label, chosen.get("name"), chosen.get("id"), board_id,
                    )
                    return chosen
            logger.info(
                "_find_sprint: no sprint matched %r across %d board(s) (%d sprints)",
                sprint_label, len(boards), len(aggregated),
            )
            return None
        except Exception as exc:
            logger.warning("_find_sprint failed for %s/%s: %s", project_key, sprint_label, exc)
            return None

    async def resolve_sprint_id(self, project_key: str, sprint_label: str) -> Optional[int]:
        """Resolve a sprint label to a sprint id for *project_key*'s board.

        Accepts 'Active Sprint', 'Next Sprint', or an explicit sprint name
        (e.g. 'PCF-BANK 26.07.A'). Future sprints are ordered chronologically by the
        'YY.MM.A/B/C' naming convention so 'Next Sprint' picks the nearest upcoming one.

        Returns None if no board/sprint matches. Never raises — assignment is best-effort.
        """
        sprint = await self._find_sprint(project_key, sprint_label)
        return int(sprint["id"]) if sprint else None

    async def resolve_sprint_object(
        self, project_key: str, sprint_label: str
    ) -> Optional[Dict[str, Any]]:
        """Like resolve_sprint_id but returns the full sprint dict {id, name, state}.

        Use this when both the sprint ID (for JQL) and the sprint name (for display)
        are needed. Never raises — returns None on any error.
        """
        return await self._find_sprint(project_key, sprint_label)

    async def get_sprint_context(
        self, project_key: str, max_future: int = 3
    ) -> Optional[str]:
        """Return a human-readable sprint summary for *project_key*.

        Fetches active + future sprints from the project's Agile board(s) and
        formats them so the orchestrator LLM can show real sprint names to the user
        instead of blind labels like "Active Sprint" / "Next Sprint".

        Returns a formatted string like:
            Active : PCF-BANK 26.06.B (id=42)
            Future : PCF-BANK 26.07.A (id=43) ← Next Sprint
                     PCF-BANK 26.07.B (id=44)
        Returns None if no board / sprint data is available. Never raises.
        """
        try:
            boards = await self.get_boards_for_project(project_key)
            if not boards:
                return None
            boards.sort(key=lambda b: 0 if b.get("type") == "scrum" else 1)

            seen_ids: set = set()
            all_sprints: List[Dict[str, Any]] = []
            for board in boards:
                bid = board.get("id")
                if bid is None:
                    continue
                for s in await self.get_board_sprints(bid):
                    if s.get("id") not in seen_ids:
                        seen_ids.add(s.get("id"))
                        all_sprints.append(s)

            actives = [s for s in all_sprints if s.get("state") == "active"]
            futures = sorted(
                [s for s in all_sprints if s.get("state") == "future"],
                key=_sprint_sort_key,
            )

            if not actives and not futures:
                return None

            lines: List[str] = []
            for s in actives:
                lines.append(f"Active  : {s['name']} (id={s['id']})")
            for i, s in enumerate(futures[:max_future]):
                tag = " ← Next Sprint" if i == 0 else ""
                lines.append(f"Future  : {s['name']} (id={s['id']}){tag}")

            return "\n".join(lines)
        except Exception as exc:
            logger.warning("get_sprint_context(%s) failed — %s", project_key, exc)
            return None

    # ── Create ──────────────────────────────────────────────────────────────

    @with_retry(max_attempts=3, backoff_base=1.0, exceptions=(httpx.HTTPError,))
    async def create_issue(self, fields: Dict[str, Any]) -> str:
        """Create a Jira issue. Returns the new issue key (e.g. 'EWL-123').

        Atomic: either the ticket is fully created or nothing happens.
        """
        url = f"{self._base}/issue"
        logger.debug("Jira create_issue → %s | issuetype=%s summary=%s",
                     url, fields.get("issuetype"), fields.get("summary"))
        resp = await self._http.post(url, json={"fields": fields})
        if resp.status_code == 401:
            raise JiraError(_401_HINT)
        if resp.status_code not in (200, 201):
            try:
                body = resp.json()
                errors = body.get("errors", {}) or {}
                error_messages = body.get("errorMessages", [])
            except Exception:
                errors, error_messages = {}, [resp.text[:400]]

            if resp.status_code == 400 and errors:
                raise JiraFieldError(errors, fields)

            raise JiraError(
                f"create_issue failed → HTTP {resp.status_code}: {errors or error_messages}"
            )
        return resp.json()["key"]

    # ── Update (confirmation required) ──────────────────────────────────────

    async def update_issue(self, issue_key: str, changes: Dict[str, Any]) -> None:
        """Public update path — ALWAYS raises UpdateConfirmationRequired."""
        require_update_confirmation(issue_key, changes)

    @with_retry(max_attempts=3, backoff_base=1.0, exceptions=(httpx.HTTPError,))
    async def _update_issue_confirmed(
        self,
        issue_key: str,
        changes: Dict[str, Any],
        user_email: str = "",
    ) -> None:
        """Execute update ONLY after user confirmation AND authorization check.

        *user_email*: the Teams user's email extracted from the activity.
        If provided, enforces that the user is reporter or assignee of the issue.
        """
        from app.core.authz import assert_can_modify_issue
        await assert_can_modify_issue(self, issue_key, user_email)

        url = f"{self._base}/issue/{issue_key}"
        resp = await self._http.put(url, json={"fields": changes})
        if resp.status_code == 401:
            raise JiraError(_401_HINT)
        if resp.status_code not in (200, 204):
            raise JiraError(
                f"update_issue({issue_key}) → HTTP {resp.status_code}: {resp.text[:300]}"
            )

    # ── Field helpers ────────────────────────────────────────────────────────

    def build_fields(
        self,
        ticket: Dict[str, Any],
        reporter_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert a JiraTicket-shaped dict to Jira REST API fields payload.

        *reporter_name*: Jira username to set as reporter. If None, the field
        is omitted and Jira defaults to the authenticated user (the bot).
        """
        fields: Dict[str, Any] = {
            "project": {"key": ticket["project_key"]},
            "issuetype": {"name": ticket["issue_type"]},
            "summary": ticket["summary"],
            "description": self._format_description(ticket.get("description") or {}),
            "priority": {
                "name": _PRIORITY_MAP.get(
                    ticket.get("priority", "P3 (Medium)"), "Medium"
                )
            },
        }

        if reporter_name:
            fields["reporter"] = {"name": reporter_name}

        if ticket.get("assignee"):
            fields["assignee"] = {"name": ticket["assignee"]}

        if ticket["issue_type"] == "Task" and ticket.get("task_category"):
            fields[self._task_category_field] = {"value": ticket["task_category"]}

        if ticket["issue_type"] == "Epic":
            fields[self._epic_name_field] = ticket["summary"]

        epic_link = ticket.get("epic_link")
        if epic_link and ticket["issue_type"] in ("Story", "Task"):
            fields[self._epic_link_field] = epic_link

        if ticket.get("story_points") is not None:
            fields[self._story_points_field] = ticket["story_points"]

        return fields

    def build_update_fields(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """Map a user-facing change-set to a Jira REST 'fields' payload for UPDATE.

        Supported keys: summary, priority, assignee, story_points, sprint (int id),
        epic_link. Unknown keys are ignored. 'sprint' must already be a resolved
        integer sprint id (resolve via resolve_sprint_id before calling).
        """
        fields: Dict[str, Any] = {}
        if "summary" in changes and changes["summary"] is not None:
            fields["summary"] = changes["summary"]
        if "priority" in changes and changes["priority"] is not None:
            fields["priority"] = {
                "name": _PRIORITY_MAP.get(changes["priority"], changes["priority"])
            }
        if "assignee" in changes and changes["assignee"] is not None:
            fields["assignee"] = {"name": changes["assignee"]}
        if "story_points" in changes and changes["story_points"] is not None:
            fields[self._story_points_field] = changes["story_points"]
        if "sprint" in changes and changes["sprint"] is not None:
            fields[self._sprint_field] = changes["sprint"]
        if "epic_link" in changes and changes["epic_link"] is not None:
            fields[self._epic_link_field] = changes["epic_link"]
        return fields

    @staticmethod
    def _format_description(desc: Dict[str, Any]) -> str:
        """Render ticket description as Jira wiki markup."""
        if not desc:
            return ""
        req_type = desc.get("requirement_type", "")
        return (
            f"h2. 1. Context\n{desc.get('context', '')}\n\n"
            f"h2. 2. Requirement\n"
            f"h3. {req_type}\n{desc.get('requirement_content', '')}\n\n"
            f"h2. 3. Acceptance Criteria\n{desc.get('acceptance_criteria', '')}"
        )

    def update_field_ids(
        self,
        epic_name_field: Optional[str] = None,
        epic_link_field: Optional[str] = None,
        story_points_field: Optional[str] = None,
        sprint_field: Optional[str] = None,
        task_category_field: Optional[str] = None,
    ) -> None:
        """Hot-update field IDs without recreating the HTTP client."""
        if epic_name_field:
            self._epic_name_field = epic_name_field
        if epic_link_field:
            self._epic_link_field = epic_link_field
        if story_points_field:
            self._story_points_field = story_points_field
        if sprint_field:
            self._sprint_field = sprint_field
        if task_category_field:
            self._task_category_field = task_category_field

    async def aclose(self) -> None:
        await self._http.aclose()


# ── Singleton factory ────────────────────────────────────────────────────────

# Maps email (lowercase) → Jira username, or None if not found
_jira_username_cache: Dict[str, Optional[str]] = {}
# Cached bot username (from /myself)
_bot_username: Optional[str] = None

_client: Optional[JiraClient] = None


def get_jira_client() -> JiraClient:
    """Return a module-level singleton JiraClient built from current settings.

    API token is resolved from AgentBase at startup (via secrets.get_secret),
    falling back to JIRA_API_TOKEN in .env.
    """
    global _client
    if _client is None:
        from app.config import settings
        from app.core.secrets import get_secret
        _client = JiraClient(
            server_url=settings.jira_server_url,
            user_email=settings.jira_user_email,
            api_token=get_secret("jira-api-key", settings.jira_api_token),
            epic_link_field=settings.jira_epic_link_field,
            epic_name_field=settings.jira_epic_name_field,
            story_points_field=settings.jira_story_points_field,
            sprint_field=settings.jira_sprint_field,
            host_header=settings.jira_host_header,
        )
    return _client


def reset_jira_client() -> None:
    """Force singleton rebuild on next get_jira_client() call (useful after .env change)."""
    global _client
    _client = None


def reset_jira_username_cache() -> None:
    """Clear the in-process Jira username cache (useful in tests)."""
    global _bot_username
    _jira_username_cache.clear()
    _bot_username = None
