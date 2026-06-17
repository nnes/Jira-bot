"""Tests for Jira operation guardrails."""
import pytest

from app.core.errors import ForbiddenOperationError, UpdateConfirmationRequired
from app.integrations.jira.guards import assert_no_delete, require_update_confirmation


class TestAssertNoDelete:
    def test_delete_raises(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("delete")

    def test_delete_case_insensitive(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("DELETE")

    def test_destroy_raises(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("destroy")

    def test_purge_raises(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("purge")

    def test_remove_raises(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("remove")

    def test_archive_raises(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("archive")

    def test_bulk_delete_raises(self):
        with pytest.raises(ForbiddenOperationError):
            assert_no_delete("bulk_delete")

    def test_create_allowed(self):
        # Should not raise
        assert_no_delete("create")

    def test_read_allowed(self):
        assert_no_delete("read")

    def test_get_allowed(self):
        assert_no_delete("get")


class TestRequireUpdateConfirmation:
    def test_always_raises(self):
        with pytest.raises(UpdateConfirmationRequired):
            require_update_confirmation("EWL-1", {"status": "Done"})

    def test_carries_issue_key(self):
        with pytest.raises(UpdateConfirmationRequired) as exc_info:
            require_update_confirmation("EWL-42", {"priority": "P1"})
        assert exc_info.value.issue_key == "EWL-42"

    def test_carries_changes(self):
        changes = {"summary": "New summary"}
        with pytest.raises(UpdateConfirmationRequired) as exc_info:
            require_update_confirmation("EWL-99", changes)
        assert exc_info.value.changes == changes


class TestJiraClientNoDeleteMethods:
    """Verify that JiraClient does not expose any delete/destroy method."""

    def test_no_delete_method(self):
        from app.integrations.jira.client import JiraClient
        client_attrs = dir(JiraClient)
        forbidden = {"delete", "destroy", "purge", "remove", "bulk_delete", "archive", "trash"}
        exposed = {a for a in client_attrs if not a.startswith("_")}
        assert not (exposed & forbidden), (
            f"JiraClient must not expose destructive methods; found: {exposed & forbidden}"
        )
