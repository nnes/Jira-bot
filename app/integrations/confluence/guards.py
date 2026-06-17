"""Confluence operation guardrails — enforce STRICTLY READ-ONLY access.

Confluence integration must never create, modify, or delete pages. Any code path
that could mutate Confluence must call assert_read_only() first.
"""
from app.core.errors import ConfluenceForbidden

# Only these operations are ever permitted against Confluence.
_ALLOWED_OPS = frozenset({"read", "get", "fetch", "search", "view", "list"})

# Explicitly forbidden (for clear error messages / detection).
_FORBIDDEN_OPS = frozenset({
    "create", "update", "modify", "edit", "delete", "remove",
    "destroy", "purge", "archive", "trash", "move", "publish",
})


def assert_read_only(operation: str) -> None:
    """Raise ConfluenceForbidden unless *operation* is a read-only operation.

    Called at the top of any Confluence code path before issuing a request.
    """
    if operation.lower() not in _ALLOWED_OPS:
        raise ConfluenceForbidden(operation)
