"""Tests for Jira statistics: JQL building, aggregation, intent detection (Feature 2)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.graph.nodes.orchestrator import _is_stats_request
from app.graph.nodes.stats import aggregate_issues, build_stats_jql
from app.integrations.jira.client import JiraClient


# ── build_stats_jql ────────────────────────────────────────────────────────────

class TestBuildStatsJql:
    def test_full_spec(self):
        jql = build_stats_jql({
            "project_key": "EWL",
            "completed_only": True,
            "issue_types": ["Story", "Task"],
            "date_field": "resolved",
            "date_from": "2026-06-01",
            "date_to": "2026-06-30",
        })
        assert 'project = "EWL"' in jql
        assert 'statusCategory = Done' in jql
        assert 'issuetype in ("Story", "Task")' in jql
        assert 'resolved >= "2026-06-01"' in jql
        assert 'resolved <= "2026-06-30"' in jql

    def test_assignee_and_default_resolved_field(self):
        # completed_only with date but no explicit date_field → defaults to resolved
        jql = build_stats_jql({
            "assignee": "an.nguyen",
            "completed_only": True,
            "date_from": "2026-06-01",
        })
        assert 'assignee = "an.nguyen"' in jql
        assert 'resolved >= "2026-06-01"' in jql

    def test_explicit_statuses_when_not_completed(self):
        jql = build_stats_jql({"project_key": "EWL", "statuses": ["Done", "Closed"]})
        assert 'status in ("Done", "Closed")' in jql
        assert "statusCategory" not in jql

    def test_empty_spec_returns_empty(self):
        assert build_stats_jql({}) == ""

    def test_quotes_are_stripped_to_prevent_injection(self):
        jql = build_stats_jql({"project_key": 'EWL" OR project = "X'})
        assert jql == 'project = "EWL OR project = X"'

    def test_active_sprint(self):
        jql = build_stats_jql({"project_key": "PCFBANK", "sprint": "active"})
        assert "sprint in openSprints()" in jql
        assert 'project = "PCFBANK"' in jql

    def test_next_sprint(self):
        jql = build_stats_jql({"project_key": "PCFBANK", "sprint": "next"})
        assert "sprint in futureSprints()" in jql

    def test_explicit_sprint_name(self):
        jql = build_stats_jql({"sprint": "PCF-BANK 26.07.A"})
        assert 'sprint = "PCF-BANK 26.07.A"' in jql

    def test_no_sprint_clause_when_null(self):
        jql = build_stats_jql({"project_key": "EWL", "assignee": "bachnt"})
        assert "sprint" not in jql

    def test_active_sprint_with_assignee_and_issue_types(self):
        jql = build_stats_jql({
            "project_key": "PCFBANK",
            "assignee": "quanna3",
            "issue_types": ["Story", "Epic"],
            "sprint": "active",
        })
        assert 'project = "PCFBANK"' in jql
        assert 'assignee = "quanna3"' in jql
        assert 'issuetype in ("Story", "Epic")' in jql
        assert "sprint in openSprints()" in jql

    def test_sprint_id_overrides_jql_function(self):
        # When sprint_id is present (resolved from Agile API), use concrete ID not function
        jql = build_stats_jql({
            "project_key": "PCFBANK",
            "assignee": "TuLHM",
            "issue_types": ["Task"],
            "sprint": "active",
            "sprint_id": 77,
        })
        assert "sprint = 77" in jql
        assert "openSprints" not in jql

    def test_sprint_id_with_next(self):
        jql = build_stats_jql({
            "project_key": "PCFBANK",
            "sprint": "next",
            "sprint_id": 99,
        })
        assert "sprint = 99" in jql
        assert "futureSprints" not in jql

    def test_sprint_id_zero_still_uses_id(self):
        # sprint_id=0 is falsy in Python but still a valid ID
        jql = build_stats_jql({
            "project_key": "PCFBANK",
            "sprint": "active",
            "sprint_id": 0,
        })
        assert "sprint = 0" in jql


# ── aggregate_issues ───────────────────────────────────────────────────────────

class TestAggregateIssues:
    def test_counts_and_sum(self):
        issues = [
            {"fields": {"issuetype": {"name": "Story"}, "status": {"name": "Done"}, "customfield_10016": 5}},
            {"fields": {"issuetype": {"name": "Story"}, "status": {"name": "Done"}, "customfield_10016": 3}},
            {"fields": {"issuetype": {"name": "Task"}, "status": {"name": "In Progress"}, "customfield_10016": None}},
        ]
        agg = aggregate_issues(issues, "customfield_10016")
        assert agg["by_type"] == {"Story": 2, "Task": 1}
        assert agg["by_status"] == {"Done": 2, "In Progress": 1}
        assert agg["total_points"] == 8

    def test_empty(self):
        agg = aggregate_issues([], "customfield_10016")
        assert agg == {"by_type": {}, "by_status": {}, "total_points": 0}

    def test_float_points_preserved(self):
        issues = [{"fields": {"issuetype": {"name": "Story"}, "status": {"name": "Done"}, "customfield_10016": 2.5}}]
        agg = aggregate_issues(issues, "customfield_10016")
        assert agg["total_points"] == 2.5


# ── search_issues (read-only JQL) ───────────────────────────────────────────────

def _make_client() -> JiraClient:
    return JiraClient(server_url="http://jira.local:8080", user_email="", api_token="pat")


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = ""
    return r


@pytest.mark.asyncio
async def test_search_issues_single_page():
    client = _make_client()
    client._http = AsyncMock()
    client._http.post.return_value = _resp(200, {
        "total": 2,
        "issues": [{"key": "EWL-1"}, {"key": "EWL-2"}],
    })
    result = await client.search_issues("project = EWL", fields=["issuetype"])
    assert result["total"] == 2
    assert len(result["issues"]) == 2
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_search_issues_truncates_at_cap():
    client = _make_client()
    client._http = AsyncMock()
    # total=300 but cap max_results=100 → one page, truncated
    client._http.post.return_value = _resp(200, {
        "total": 300,
        "issues": [{"key": f"EWL-{i}"} for i in range(100)],
    })
    result = await client.search_issues("project = EWL", max_results=100, page_size=100)
    assert result["total"] == 300
    assert len(result["issues"]) == 100
    assert result["truncated"] is True


# ── intent detection ─────────────────────────────────────────────────────────

class TestStatsIntent:
    @pytest.mark.parametrize("text", [
        "thống kê số ticket project EWL",
        "user an.nguyen làm xong bao nhiêu task tháng này",
        "báo cáo story points sprint này",
        "how many epics in PCFBANK",
        "số lượng bug đã đóng của EWL",
    ])
    def test_detects_stats(self, text):
        assert _is_stats_request(text) is True

    @pytest.mark.parametrize("text", [
        "tạo story tổng hợp báo cáo doanh thu",  # create verb suppresses
        "tạo task thống kê giao dịch",
        "đổi story point EWL-1 thành 5",
        "tóm tắt confluence này",
    ])
    def test_ignores_non_stats(self, text):
        assert _is_stats_request(text) is False
