"""Tests for app.integrations.jira.field_sync — field parsing helpers."""
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.jira.field_sync import (
    _find_special_fields,
    _parse_fields,
)


# ── Sample createmeta fixture ─────────────────────────────────────────────────

MOCK_CREATEMETA: Dict[str, Any] = {
    "projects": [
        {
            "key": "EWL",
            "name": "E-Wallet",
            "issuetypes": [
                {
                    "name": "Epic",
                    "fields": {
                        "summary": {"name": "Summary", "required": True},
                        "customfield_10103": {"name": "Epic Name", "required": True},
                    },
                },
                {
                    "name": "Story",
                    "fields": {
                        "summary": {"name": "Summary", "required": True},
                        "customfield_10101": {"name": "Epic Link", "required": False},
                        "customfield_10016": {"name": "Story Points", "required": False},
                        "customfield_10007": {"name": "Sprint", "required": False},
                    },
                },
                {
                    "name": "Task",
                    "fields": {
                        "summary": {"name": "Summary", "required": True},
                        "customfield_10101": {"name": "Epic Link", "required": False},
                        "customfield_10016": {"name": "Story Points", "required": False},
                        "customfield_10007": {"name": "Sprint", "required": False},
                    },
                },
            ],
        }
    ]
}


# ── _parse_fields ─────────────────────────────────────────────────────────────

class TestParseFields:
    def test_returns_dict_keyed_by_issuetype(self):
        result = _parse_fields(MOCK_CREATEMETA)
        assert "Epic" in result
        assert "Story" in result
        assert "Task" in result

    def test_epic_has_epic_name_field(self):
        result = _parse_fields(MOCK_CREATEMETA)
        assert "customfield_10103" in result["Epic"]
        assert result["Epic"]["customfield_10103"]["name"] == "Epic Name"
        assert result["Epic"]["customfield_10103"]["required"] is True

    def test_story_has_epic_link_field(self):
        result = _parse_fields(MOCK_CREATEMETA)
        assert "customfield_10101" in result["Story"]
        assert result["Story"]["customfield_10101"]["name"] == "Epic Link"

    def test_story_has_story_points_field(self):
        result = _parse_fields(MOCK_CREATEMETA)
        assert "customfield_10016" in result["Story"]
        assert result["Story"]["customfield_10016"]["required"] is False

    def test_empty_projects_returns_empty_dict(self):
        result = _parse_fields({"projects": []})
        assert result == {}

    def test_missing_fields_key_handled(self):
        data = {"projects": [{"issuetypes": [{"name": "Bug"}]}]}
        result = _parse_fields(data)
        assert result.get("Bug") == {}


# ── _find_special_fields ──────────────────────────────────────────────────────

class TestFindSpecialFields:
    def test_finds_epic_name_id(self):
        fields_by_type = _parse_fields(MOCK_CREATEMETA)
        epic_name_id, _, _, _ = _find_special_fields(fields_by_type)
        assert epic_name_id == "customfield_10103"

    def test_finds_epic_link_id(self):
        fields_by_type = _parse_fields(MOCK_CREATEMETA)
        _, epic_link_id, _, _ = _find_special_fields(fields_by_type)
        assert epic_link_id == "customfield_10101"

    def test_finds_story_points_id(self):
        fields_by_type = _parse_fields(MOCK_CREATEMETA)
        _, _, story_points_id, _ = _find_special_fields(fields_by_type)
        assert story_points_id == "customfield_10016"

    def test_finds_sprint_id(self):
        fields_by_type = _parse_fields(MOCK_CREATEMETA)
        _, _, _, sprint_id = _find_special_fields(fields_by_type)
        assert sprint_id == "customfield_10007"

    def test_missing_epic_type_returns_none_for_name(self):
        fields_by_type = {
            "Story": {
                "customfield_10101": {"name": "Epic Link", "required": False},
                "customfield_10016": {"name": "Story Points", "required": False},
            }
        }
        epic_name_id, epic_link_id, story_points_id, sprint_id = _find_special_fields(fields_by_type)
        assert epic_name_id is None
        assert epic_link_id == "customfield_10101"
        assert story_points_id == "customfield_10016"
        assert sprint_id is None

    def test_empty_dict_returns_all_none(self):
        epic_name_id, epic_link_id, story_points_id, sprint_id = _find_special_fields({})
        assert epic_name_id is None
        assert epic_link_id is None
        assert story_points_id is None
        assert sprint_id is None

    def test_non_matching_names_return_none(self):
        fields_by_type = {
            "Epic": {"customfield_99999": {"name": "Random Field", "required": False}},
            "Story": {"customfield_88888": {"name": "Custom Priority", "required": False}},
        }
        epic_name_id, epic_link_id, story_points_id, sprint_id = _find_special_fields(fields_by_type)
        assert epic_name_id is None
        assert epic_link_id is None
        assert story_points_id is None
        assert sprint_id is None


# ── auto_sync retry flow ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_sync_updates_jira_client():
    """auto_sync should call update_field_ids on the JiraClient singleton."""
    mock_jira = MagicMock()
    mock_jira.update_field_ids = MagicMock()

    with (
        patch(
            "app.integrations.jira.field_sync._fetch_createmeta",
            new=AsyncMock(return_value=MOCK_CREATEMETA),
        ),
        # get_jira_client is imported lazily inside auto_sync — patch the source module
        patch(
            "app.integrations.jira.client.get_jira_client",
            return_value=mock_jira,
        ),
    ):
        from app.integrations.jira.field_sync import auto_sync

        epic_name_id, epic_link_id, story_points_id, sprint_id = await auto_sync("EWL")

    assert epic_name_id == "customfield_10103"
    assert epic_link_id == "customfield_10101"
    assert story_points_id == "customfield_10016"
    assert sprint_id == "customfield_10007"
    mock_jira.update_field_ids.assert_called_once_with(
        "customfield_10103", "customfield_10101", "customfield_10016", "customfield_10007"
    )


@pytest.mark.asyncio
async def test_auto_sync_returns_none_on_fetch_failure():
    """auto_sync should return all-None if createmeta fetch fails."""
    with patch(
        "app.integrations.jira.field_sync._fetch_createmeta",
        new=AsyncMock(side_effect=RuntimeError("timeout")),
    ):
        from app.integrations.jira.field_sync import auto_sync

        epic_name_id, epic_link_id, story_points_id, sprint_id = await auto_sync("EWL")

    assert epic_name_id is None
    assert epic_link_id is None
    assert story_points_id is None
    assert sprint_id is None
