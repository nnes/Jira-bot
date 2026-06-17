from typing import Any, Dict


class JiraError(Exception):
    """Base class for Jira integration errors."""


class ForbiddenOperationError(JiraError):
    """Raised when a forbidden Jira operation is attempted (e.g., delete)."""


class PermissionDeniedError(JiraError):
    """Raised when the current Teams user lacks permission to modify a Jira issue.

    A user may only update issues where they are the reporter or assignee.
    """

    def __init__(self, user_email: str, issue_key: str, reporter: str, assignee: str) -> None:
        self.user_email = user_email
        self.issue_key = issue_key
        self.reporter = reporter
        self.assignee = assignee
        super().__init__(
            f"Bạn ({user_email}) không có quyền update ticket {issue_key}. "
            f"Chỉ reporter ({reporter}) hoặc assignee ({assignee or 'Unassigned'}) được phép."
        )


class StatsPermissionDeniedError(JiraError):
    """Raised when user lacks Project Lead or Admin role to view Jira stats."""

    def __init__(self, user_email: str, project_key: str) -> None:
        self.user_email = user_email
        self.project_key = project_key
        super().__init__(
            f"Bạn ({user_email}) không có quyền xem thống kê project {project_key}. "
            "Chỉ Project Lead hoặc Administrators được phép sử dụng tính năng này."
        )


class JiraFieldError(JiraError):
    """Raised on HTTP 400 when Jira rejects a field (missing required, wrong screen, etc.)."""

    def __init__(self, errors: Dict[str, Any], fields: Dict[str, Any]) -> None:
        self.errors = errors      # Jira's error dict, e.g. {'customfield_10103': 'Epic Name is required.'}
        self.fields = fields      # the fields payload that caused the error
        super().__init__(f"Jira field error: {errors}")


class JiraIssueNotFound(JiraError):
    """Raised when a Jira issue key does not exist on the server."""

    def __init__(self, issue_key: str) -> None:
        self.issue_key = issue_key
        super().__init__(f"Jira issue '{issue_key}' not found")


class UpdateConfirmationRequired(JiraError):
    """Raised when an UPDATE operation requires explicit user confirmation before execution."""

    def __init__(self, issue_key: str, changes: Dict[str, Any]) -> None:
        self.issue_key = issue_key
        self.changes = changes
        super().__init__(f"Update to '{issue_key}' requires user confirmation")


class ConfluenceUnavailable(Exception):
    """Raised when a Confluence page cannot be fetched (network error, 403, 404, etc.)."""

    def __init__(self, url: str, reason: str = "") -> None:
        self.url = url
        msg = f"Confluence page unavailable: {url}"
        if reason:
            msg += f" — {reason}"
        super().__init__(msg)


class ConfluenceForbidden(Exception):
    """Raised when a non-read Confluence operation is attempted (create/update/delete).

    Confluence access is STRICTLY READ-ONLY — this enforces that contract.
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(
            f"Confluence operation '{operation}' is not permitted. "
            "Confluence access is strictly read-only on this agent."
        )


class LLMError(Exception):
    """Raised on unrecoverable LLM provider errors after retries are exhausted."""
