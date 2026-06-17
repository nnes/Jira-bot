from typing import Any, Dict

from app.core.errors import ForbiddenOperationError, UpdateConfirmationRequired

# Operations that are absolutely forbidden on Jira
_FORBIDDEN_OPS = frozenset({
    "delete", "destroy", "purge", "remove",
    "bulk_delete", "archive", "trash",
})


def assert_no_delete(operation: str) -> None:
    """Raise ForbiddenOperationError if the operation is destructive.

    Called at the top of any Jira client method that could modify or delete data.
    """
    if operation.lower() in _FORBIDDEN_OPS:
        raise ForbiddenOperationError(
            f"Operation '{operation}' is not permitted. "
            "Jira guardrail: only Read and Create are allowed on this agent."
        )


def require_update_confirmation(issue_key: str, changes: Dict[str, Any]) -> None:
    """Enforce that every UPDATE goes through explicit user confirmation.

    Always raises UpdateConfirmationRequired. The bot layer must catch this,
    present a diff to the user, collect "có/yes", then call the internal
    confirmed update path.
    """
    raise UpdateConfirmationRequired(issue_key=issue_key, changes=changes)
