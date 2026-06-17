"""Tests for sprint name parsing + selection by the XXXX YY.MM.A/B/C convention."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.jira.client import (
    JiraClient,
    _parse_sprint_name_key,
    _select_sprint,
)

_SPRINTS = [
    {"id": 1, "name": "PCF-BANK 26.06.B", "state": "active"},
    {"id": 2, "name": "PCF-BANK 26.08.A", "state": "future"},
    {"id": 3, "name": "PCF-BANK 26.07.A", "state": "future"},
    {"id": 4, "name": "PCF-BANK 26.07.B", "state": "future"},
]


class TestParseSprintNameKey:
    def test_parses_convention(self):
        assert _parse_sprint_name_key("PCF-BANK 26.06.B") == (26, 6, ord("B"))
        assert _parse_sprint_name_key("EWL 26.12.A") == (26, 12, ord("A"))

    def test_unparseable_returns_none(self):
        assert _parse_sprint_name_key("Sprint 1") is None
        assert _parse_sprint_name_key("") is None


class TestSelectSprint:
    def test_active(self):
        assert _select_sprint(_SPRINTS, "Active Sprint")["id"] == 1

    def test_next_picks_earliest_future_not_api_order(self):
        # API order has 26.08.A first, but 26.07.A is chronologically next
        assert _select_sprint(_SPRINTS, "Next Sprint")["id"] == 3

    def test_explicit_name_exact(self):
        assert _select_sprint(_SPRINTS, "PCF-BANK 26.07.B")["id"] == 4

    def test_explicit_name_normalised_spacing_and_case(self):
        assert _select_sprint(_SPRINTS, "pcf-bank  26.08.a")["id"] == 2

    def test_no_match_returns_none(self):
        assert _select_sprint(_SPRINTS, "NONEXIST 99.99.Z") is None

    def test_empty_label_returns_none(self):
        assert _select_sprint(_SPRINTS, "") is None

    def test_next_with_no_future_returns_none(self):
        only_active = [{"id": 1, "name": "X 26.06.A", "state": "active"}]
        assert _select_sprint(only_active, "Next Sprint") is None


def _make_client() -> JiraClient:
    return JiraClient(server_url="http://jira.local:8080", user_email="", api_token="pat")


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = ""
    return r


@pytest.mark.asyncio
async def test_resolve_sprint_id_next_uses_chronological_order():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = [
        _resp(200, {"values": [{"id": 10}]}),                       # board lookup
        _resp(200, {"values": _SPRINTS, "isLast": True}),           # board sprints
    ]
    sprint_id = await client.resolve_sprint_id("PCFBANK", "Next Sprint")
    assert sprint_id == 3  # 26.07.A


@pytest.mark.asyncio
async def test_resolve_sprint_id_explicit_name():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = [
        _resp(200, {"values": [{"id": 10}]}),
        _resp(200, {"values": _SPRINTS, "isLast": True}),
    ]
    sprint_id = await client.resolve_sprint_id("PCFBANK", "PCF-BANK 26.08.A")
    assert sprint_id == 2


@pytest.mark.asyncio
async def test_resolve_sprint_id_no_board_returns_none():
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.return_value = _resp(200, {"values": []})  # no boards
    assert await client.resolve_sprint_id("PCFBANK", "Next Sprint") is None


@pytest.mark.asyncio
async def test_resolve_sprint_id_searches_multiple_boards_scrum_first():
    """A project may have a kanban board (no sprints) + a scrum board — must find the sprint."""
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = [
        # boards list: kanban first, scrum second — resolver must try scrum
        _resp(200, {"values": [
            {"id": 1, "type": "kanban"},
            {"id": 2, "type": "scrum"},
        ]}),
        _resp(200, {"values": _SPRINTS, "isLast": True}),   # scrum board (id=2) sprints
    ]
    sprint_id = await client.resolve_sprint_id("PCFBANK", "PCF-BANK 26.07.A")
    assert sprint_id == 3


@pytest.mark.asyncio
async def test_resolve_sprint_id_aggregates_across_boards():
    """If the first board lacks the sprint, keep searching subsequent boards."""
    client = _make_client()
    client._http = AsyncMock()
    client._http.get.side_effect = [
        _resp(200, {"values": [{"id": 1, "type": "scrum"}, {"id": 2, "type": "scrum"}]}),
        _resp(200, {"values": [{"id": 50, "name": "OTHER 26.06.A", "state": "future"}], "isLast": True}),
        _resp(200, {"values": [{"id": 60, "name": "PCF-BANK 26.07.A", "state": "future"}], "isLast": True}),
    ]
    sprint_id = await client.resolve_sprint_id("PCFBANK", "PCF-BANK 26.07.A")
    assert sprint_id == 60


@pytest.mark.asyncio
async def test_move_issue_to_sprint_uses_agile_api():
    client = _make_client()
    client._http = AsyncMock()
    resp = MagicMock(); resp.status_code = 204; resp.text = ""
    client._http.post.return_value = resp
    await client.move_issue_to_sprint(777, "PCFBANK-10180")
    url, kwargs = client._http.post.call_args
    assert url[0].endswith("/rest/agile/1.0/sprint/777/issue")
    assert kwargs["json"] == {"issues": ["PCFBANK-10180"]}


@pytest.mark.asyncio
async def test_move_issue_to_sprint_raises_on_error():
    from app.core.errors import JiraError
    client = _make_client()
    client._http = AsyncMock()
    resp = MagicMock(); resp.status_code = 400; resp.text = "bad sprint"
    client._http.post.return_value = resp
    with pytest.raises(JiraError):
        await client.move_issue_to_sprint(777, "PCFBANK-1")


@pytest.mark.asyncio
async def test_get_board_sprints_paginates():
    client = _make_client()
    client._http = AsyncMock()
    page1 = [{"id": i, "name": f"X 26.06.{chr(65+i)}", "state": "future"} for i in range(50)]
    page2 = [{"id": 99, "name": "X 26.07.A", "state": "future"}]
    client._http.get.side_effect = [
        _resp(200, {"values": page1, "isLast": False}),
        _resp(200, {"values": page2, "isLast": True}),
    ]
    sprints = await client.get_board_sprints(10)
    assert len(sprints) == 51
