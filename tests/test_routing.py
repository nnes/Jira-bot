"""Tests for app.graph.routing — conditional edge logic."""
import pytest

from app.graph.routing import route_after_orchestrator
from app.graph.state import empty_state


def _state(**overrides):
    s = empty_state()
    s.update(overrides)
    return s


class TestRouteAfterOrchestrator:
    def test_not_ready_returns_end(self):
        state = _state(ready_to_generate=False)
        assert route_after_orchestrator(state) == "end"

    def test_ready_no_confluence_returns_generator(self):
        state = _state(ready_to_generate=True, confluence_url=None)
        assert route_after_orchestrator(state) == "generator"

    def test_ready_with_confluence_returns_reranker(self):
        state = _state(
            ready_to_generate=True,
            confluence_url="https://confluence.example.com/display/EW/PRD",
        )
        assert route_after_orchestrator(state) == "reranker"

    def test_ready_empty_confluence_url_returns_generator(self):
        # Empty string is falsy — should not route to reranker
        state = _state(ready_to_generate=True, confluence_url="")
        assert route_after_orchestrator(state) == "generator"

    def test_not_ready_with_confluence_returns_end(self):
        # Even if confluence_url is set, if not ready → end
        state = _state(
            ready_to_generate=False,
            confluence_url="https://confluence.example.com/display/EW/PRD",
        )
        assert route_after_orchestrator(state) == "end"

    def test_ready_to_update_returns_updater(self):
        state = _state(ready_to_update=True)
        assert route_after_orchestrator(state) == "updater"

    def test_update_takes_precedence_over_generate(self):
        # If both flags somehow set, update wins (independent confirmation gates)
        state = _state(ready_to_update=True, ready_to_generate=True, confluence_url="x")
        assert route_after_orchestrator(state) == "updater"

    def test_not_ready_to_update_does_not_route_updater(self):
        state = _state(ready_to_update=False, ready_to_generate=False)
        assert route_after_orchestrator(state) == "end"

    def test_ready_for_stats_returns_stats(self):
        state = _state(ready_for_stats=True)
        assert route_after_orchestrator(state) == "stats"

    def test_stats_takes_precedence_over_generate_and_update(self):
        state = _state(ready_for_stats=True, ready_to_update=True, ready_to_generate=True)
        assert route_after_orchestrator(state) == "stats"
