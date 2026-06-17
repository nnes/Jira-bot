"""Tests for Jira Agile-API sprint resolution + update field mapping (Issue 1)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.jira.client import JiraClient


def _make_client() -> JiraClient:
    return JiraClient(
        server_url="http://jira.local:8080",
        user_email="",
        api_token="pat",
        sprint_field="customfield_10007",
        story_points_field="customfield_10016",
    )


def _resp(status: int, json_body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body
    r.text = ""
    return r


# ── resolve_sprint_id ──────────────────────────────────────────────────────────
# Note: name-convention selection (active / next / explicit name) is covered in
# test_sprint_select.py. These cover the error/edge paths.

@pytest.mark.asyncio
async def test_resolve_sprint_id_no_board_returns_none():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.return_value = _resp(200, {"values": []})  # no boards
    sprint_id = await client.resolve_sprint_id("EWL", "Active Sprint")
    assert sprint_id is None


@pytest.mark.asyncio
async def test_resolve_sprint_id_no_sprint_returns_none():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = [
        _resp(200, {"values": [{"id": 42}]}),
        _resp(200, {"values": []}),  # no active sprint
    ]
    sprint_id = await client.resolve_sprint_id("EWL", "Active Sprint")
    assert sprint_id is None


@pytest.mark.asyncio
async def test_resolve_sprint_id_swallows_errors():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = RuntimeError("boom")
    # Must never raise — sprint assignment is best-effort
    sprint_id = await client.resolve_sprint_id("EWL", "Active Sprint")
    assert sprint_id is None


# ── resolve_sprint_object ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_sprint_object_returns_full_dict():
    """resolve_sprint_object returns the full sprint dict including name and state."""
    client = _make_client()
    client._http = AsyncMock()
    active_sprint = {"id": 77, "name": "PCF-BANK 26.06.B", "state": "active"}
    client._http.get.side_effect = [
        _resp(200, {"values": [{"id": 10, "type": "scrum"}]}),  # boards
        _resp(200, {"values": [active_sprint], "isLast": True}),  # sprints
    ]
    obj = await client.resolve_sprint_object("PCFBANK", "active sprint")
    assert obj is not None
    assert obj["id"] == 77
    assert obj["name"] == "PCF-BANK 26.06.B"
    assert obj["state"] == "active"


@pytest.mark.asyncio
async def test_resolve_sprint_object_returns_none_when_not_found():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = [
        _resp(200, {"values": [{"id": 10, "type": "scrum"}]}),
        _resp(200, {"values": [], "isLast": True}),  # no sprints
    ]
    obj = await client.resolve_sprint_object("PCFBANK", "active sprint")
    assert obj is None


@pytest.mark.asyncio
async def test_resolve_sprint_id_delegates_to_find_sprint():
    """resolve_sprint_id and resolve_sprint_object resolve to the same sprint."""
    client = _make_client()
    active_sprint = {"id": 77, "name": "PCF-BANK 26.06.B", "state": "active"}

    def _side_effects():
        return [
            _resp(200, {"values": [{"id": 10, "type": "scrum"}]}),
            _resp(200, {"values": [active_sprint], "isLast": True}),
        ]

    client._http = AsyncMock()
    client._http.get.side_effect = _side_effects()
    sprint_id = await client.resolve_sprint_id("PCFBANK", "active sprint")
    assert sprint_id == 77

    client._http.get.side_effect = _side_effects()
    sprint_obj = await client.resolve_sprint_object("PCFBANK", "active sprint")
    assert sprint_obj is not None
    assert sprint_obj["id"] == 77


# ── build_update_fields ──────────────────────────────────────────────────────

class TestBuildUpdateFields:
    def test_story_points_uses_custom_field(self):
        client = _make_client()
        fields = client.build_update_fields({"story_points": 5})
        assert fields == {"customfield_10016": 5}

    def test_sprint_uses_custom_field(self):
        client = _make_client()
        fields = client.build_update_fields({"sprint": 777})
        assert fields == {"customfield_10007": 777}

    def test_assignee_maps_to_name(self):
        client = _make_client()
        fields = client.build_update_fields({"assignee": "an.nguyen"})
        assert fields == {"assignee": {"name": "an.nguyen"}}

    def test_priority_maps_via_priority_map(self):
        client = _make_client()
        fields = client.build_update_fields({"priority": "P1 (Critical)"})
        assert fields == {"priority": {"name": "Critical"}}

    def test_ignores_unknown_and_none(self):
        client = _make_client()
        fields = client.build_update_fields(
            {"unknown_key": "x", "story_points": None, "summary": "New title"}
        )
        assert fields == {"summary": "New title"}

    def test_multiple_fields(self):
        client = _make_client()
        fields = client.build_update_fields(
            {"story_points": 3, "assignee": "bob", "sprint": 12}
        )
        assert fields == {
            "customfield_10016": 3,
            "assignee": {"name": "bob"},
            "customfield_10007": 12,
        }
