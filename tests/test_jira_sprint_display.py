"""Tests for reading the Sprint field when fetching a Jira issue (multi-sprint support)."""
from app.graph.nodes.orchestrator import (
    _extract_sprints,
    _format_jira_issue,
    _parse_sprint_entry,
)


class TestParseSprintEntry:
    def test_dict_entry(self):
        assert _parse_sprint_entry({"name": "PCF-BANK 26.06.B", "state": "ACTIVE"}) == (
            "PCF-BANK 26.06.B", "active"
        )

    def test_legacy_string_entry(self):
        s = ("com.atlassian.greenhopper.service.sprint.Sprint@1"
             "[id=10,rapidViewId=2,state=CLOSED,name=PCF-BANK 26.05.B,startDate=2026-05-15]")
        assert _parse_sprint_entry(s) == ("PCF-BANK 26.05.B", "closed")

    def test_unknown_type(self):
        assert _parse_sprint_entry(12345) == (None, None)


class TestExtractSprints:
    FIELD = "customfield_10007"

    def test_object_array_multi_sprint(self):
        fields = {self.FIELD: [
            {"name": "PCF-BANK 26.05.B", "state": "closed"},
            {"name": "PCF-BANK 26.06.A", "state": "closed"},
            {"name": "PCF-BANK 26.06.B", "state": "active"},
        ]}
        assert _extract_sprints(fields, self.FIELD) == [
            ("PCF-BANK 26.05.B", "closed"),
            ("PCF-BANK 26.06.A", "closed"),
            ("PCF-BANK 26.06.B", "active"),
        ]

    def test_legacy_string_array(self):
        fields = {self.FIELD: [
            "Sprint@1[id=10,state=CLOSED,name=PCF-BANK 26.05.B]",
            "Sprint@2[id=11,state=ACTIVE,name=PCF-BANK 26.06.B]",
        ]}
        assert _extract_sprints(fields, self.FIELD) == [
            ("PCF-BANK 26.05.B", "closed"),
            ("PCF-BANK 26.06.B", "active"),
        ]

    def test_fallback_scan_when_field_id_wrong(self):
        fields = {"customfield_12345": [{"name": "X 26.06.B", "state": "active"}]}
        assert _extract_sprints(fields, "customfield_99999") == [("X 26.06.B", "active")]

    def test_no_sprint_returns_empty(self):
        assert _extract_sprints({"summary": "x"}, self.FIELD) == []


class TestFormatJiraIssueSprint:
    FIELD = "customfield_10007"

    def test_multi_sprint_rendered(self):
        issue = {"key": "PCFBANK-1", "fields": {
            "summary": "X", "issuetype": {"name": "Story"}, "status": {"name": "In Progress"},
            self.FIELD: [
                {"name": "PCF-BANK 26.05.B", "state": "closed"},
                {"name": "PCF-BANK 26.06.B", "state": "active"},
            ],
        }}
        out = _format_jira_issue(issue, self.FIELD)
        assert "Sprint: PCF-BANK 26.05.B (closed), PCF-BANK 26.06.B (active)" in out
        assert "Sprint hiện tại (active): PCF-BANK 26.06.B" in out

    def test_no_sprint_omits_line(self):
        issue = {"key": "E-1", "fields": {"summary": "Z", "issuetype": {"name": "Epic"}}}
        out = _format_jira_issue(issue, self.FIELD)
        assert "Sprint:" not in out
