from typing import Dict

from app.graph.state import AgentState, empty_state


class ConversationStore:
    """In-memory per-conversation state store.

    Keyed by activity.conversation.id so each chat thread has its own state.
    In production this should be backed by Redis or a database.
    """

    def __init__(self) -> None:
        self._store: Dict[str, AgentState] = {}

    def get(self, conv_id: str) -> AgentState:
        """Return a working copy of the state (mutations won't affect the store)."""
        stored = self._store.get(conv_id)
        if stored is None:
            return empty_state()
        # Shallow-copy top level + new list/dict for mutable fields to avoid aliasing
        return AgentState(
            messages=list(stored["messages"]),
            current_user=stored.get("current_user"),
            slots=dict(stored["slots"]),
            confluence_url=stored["confluence_url"],
            confluence_data=stored["confluence_data"],
            jira_context=stored["jira_context"],
            ticket_json=stored["ticket_json"],
            jira_issue_key=stored["jira_issue_key"],
            ready_to_generate=stored["ready_to_generate"],
            draft_shown=stored["draft_shown"],
            ready_to_update=stored["ready_to_update"],
            update_draft_shown=stored["update_draft_shown"],
            ready_for_stats=stored["ready_for_stats"],
        )

    def save(self, conv_id: str, state: AgentState) -> None:
        self._store[conv_id] = state

    def reset(self, conv_id: str) -> None:
        self._store.pop(conv_id, None)
