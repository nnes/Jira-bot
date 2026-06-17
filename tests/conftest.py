"""Shared pytest fixtures for the clawathon test suite."""
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.graph.state import AgentState, empty_state


@pytest.fixture
def base_state() -> AgentState:
    """Minimal empty AgentState for routing/node tests."""
    return empty_state()


@pytest.fixture
def ready_state() -> AgentState:
    """State with ready_to_generate=True and no Confluence URL."""
    s = empty_state()
    s["ready_to_generate"] = True
    return s


@pytest.fixture
def ready_state_with_confluence() -> AgentState:
    """State with ready_to_generate=True and a Confluence URL set."""
    s = empty_state()
    s["ready_to_generate"] = True
    s["confluence_url"] = "https://confluence.example.com/display/EW/PRD"
    return s


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch app.config.settings with a MagicMock."""
    from app import config
    mock = MagicMock()
    mock.jira_server_url = "http://jira.local:8080"
    mock.jira_user_email = ""
    mock.jira_api_token = "test-pat"
    mock.use_mock_jira = True
    mock.jira_epic_link_field = "customfield_10101"
    mock.jira_epic_name_field = "customfield_10103"
    mock.confluence_server_url = "http://confluence.local:8090"
    mock.confluence_user_email = ""
    mock.confluence_api_token = "conf-pat"
    mock.llm_base_url = "http://llm.local/v1"
    mock.llm_api_key = "test-key"
    mock.orchestrator_model = "minimax/minimax-m2.5"
    mock.reranker_model = "qwen/qwen3-reranker-8b"
    mock.generator_model = "qwen/qwen3-5-27b"
    monkeypatch.setattr(config, "settings", mock)
    return mock


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    """AsyncMock that mimics an AsyncOpenAI client."""
    client = AsyncMock()
    choice = MagicMock()
    choice.message.content = '{"summary": "Test ticket", "issue_type": "Story"}'
    client.chat.completions.create.return_value = MagicMock(choices=[choice])
    return client


@pytest.fixture
def mock_jira_client() -> AsyncMock:
    """AsyncMock that mimics a JiraClient."""
    client = AsyncMock()
    client.check_auth.return_value = {"displayName": "Test User"}
    client.create_issue.return_value = "EWL-999"
    client.get_issue.return_value = {"key": "EWL-1", "fields": {"summary": "Epic"}}
    client.build_fields.return_value = {"project": {"key": "EWL"}, "issuetype": {"name": "Story"}}
    return client
