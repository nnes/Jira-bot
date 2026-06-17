import logging

from langgraph.graph import StateGraph, END

from app.graph.nodes.generator import generator_node
from app.graph.nodes.orchestrator import orchestrator_node
from app.graph.nodes.reranker import reranker_node
from app.graph.nodes.stats import stats_node
from app.graph.nodes.updater import updater_node
from app.graph.routing import route_after_orchestrator
from app.graph.state import AgentState

logger = logging.getLogger(__name__)


def build_graph():
    """Compile the multi-node LangGraph state machine.

    Phase 3 : orchestrator
    Phase 4 : orchestrator → (conditional) → generator
    Phase 6 : orchestrator → (has confluence?) → reranker → generator → END
                                               ↘ (no)     → generator → END
    Issue 1b: orchestrator → (ready_to_update) → updater → END
    Stats   : orchestrator → (ready_for_stats) → stats → END

    When MEMORY_ID is set, attaches AgentBaseMemoryEvents as checkpointer so
    conversation state persists across restarts/scaling via AgentBase Memory.
    """
    graph = StateGraph(AgentState)

    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("reranker", reranker_node)
    graph.add_node("generator", generator_node)
    graph.add_node("updater", updater_node)
    graph.add_node("stats", stats_node)

    graph.set_entry_point("orchestrator")

    graph.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "stats": "stats",
            "updater": "updater",
            "reranker": "reranker",
            "generator": "generator",
            "end": END,
        },
    )

    graph.add_edge("reranker", "generator")
    graph.add_edge("generator", END)
    graph.add_edge("updater", END)
    graph.add_edge("stats", END)

    checkpointer = None
    try:
        from app.config import settings
        if settings.memory_id:
            from greennode_agent_bridge import AgentBaseMemoryEvents
            checkpointer = AgentBaseMemoryEvents(memory_id=settings.memory_id)
            logger.info("graph: AgentBase checkpointer attached (memory_id=%s)", settings.memory_id)
    except Exception as exc:
        logger.warning("graph: checkpointer unavailable (%s) — state is in-memory only", exc)

    return graph.compile(checkpointer=checkpointer)
