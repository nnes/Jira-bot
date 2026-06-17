from app.graph.state import AgentState


def route_after_orchestrator(state: AgentState) -> str:
    """Conditional edge after the orchestrator node.

    - ready_for_stats                → stats (read-only Jira aggregation)
    - ready_to_update                → updater (confirmed UPDATE on existing ticket)
    - ready + confluence URL present → reranker (enrich context first)
    - ready + no confluence URL      → generator (direct ticket creation)
    - not ready                      → end (wait for more user input)
    """
    if state.get("ready_for_stats"):
        return "stats"
    if state.get("ready_to_update"):
        return "updater"
    if state.get("ready_to_generate"):
        if state.get("confluence_url"):
            return "reranker"
        return "generator"
    return "end"
