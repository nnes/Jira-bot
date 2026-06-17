from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class AgentState(TypedDict):
    """Full conversation + ticket state, persisted per conversation.id."""

    messages: List[Dict[str, str]]        # {"role": "user/assistant", "content": "..."}
    current_user: Optional[Dict[str, str]]  # {"id": ..., "name": ..., "email": ...} from Teams
    slots: Dict[str, Any]                 # slot values: project_key, issue_type, summary,
                                          #   epic_link, task_category (Task only),
                                          #   use_change_template (Epic/Story), requirement_type,
                                          #   assignee, sprint, story_points, priority
    confluence_url: Optional[str]         # original Confluence URL provided by user (Phase 6)
    confluence_data: Optional[str]        # enriched content from Confluence (Phase 6)
    jira_context: Optional[str]           # fetched Jira issue content for LLM context
    ticket_json: Optional[Dict[str, Any]] # generated ticket JSON (Phase 4+)
    jira_issue_key: Optional[str]         # created Jira ticket key, e.g. "EWL-123" (Phase 5)
    ready_to_generate: bool               # True when user confirmed ticket creation
    draft_shown: bool                     # True after TICKET DRAFT was presented to user
    ready_to_update: bool                 # True when user confirmed an UPDATE (Issue 1b)
    update_draft_shown: bool              # True after UPDATE DRAFT was presented to user
    ready_for_stats: bool                 # True when a Jira statistics request was detected


def empty_state() -> AgentState:
    return AgentState(
        messages=[],
        current_user=None,
        slots={},
        confluence_url=None,
        confluence_data=None,
        jira_context=None,
        ticket_json=None,
        jira_issue_key=None,
        ready_to_generate=False,
        draft_shown=False,
        ready_to_update=False,
        update_draft_shown=False,
        ready_for_stats=False,
    )
