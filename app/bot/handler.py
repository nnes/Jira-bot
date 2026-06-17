import asyncio
import logging
from typing import List

from botbuilder.core import ActivityHandler, TurnContext
from botbuilder.schema import ChannelAccount

from app.bot.conversation_store import ConversationStore
from app.config import settings
from app.core.authz import extract_teams_user
from app.core.ratelimit import get_rate_limiter
from app.graph.builder import build_graph
from app.graph.state import empty_state

logger = logging.getLogger(__name__)


def format_jira_link(issue_key: str) -> str:
    """Build a browseable Jira issue URL from a ticket key (e.g. 'EWL-123').

    Called by the Jira integration layer (Phase 5) after a ticket is created.
    """
    base = settings.jira_server_url.rstrip("/")
    return f"{base}/browse/{issue_key}"


class OrchestratorHandler(ActivityHandler):
    def __init__(self) -> None:
        self._graph = build_graph()
        # Fallback in-memory store used when AgentBase checkpointer is not attached
        self._store = ConversationStore()

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        conv_id = turn_context.activity.conversation.id
        user_text = (turn_context.activity.text or "").strip()

        # ── Rate limiting (120/min default): under limit → proceed; over limit →
        # notify user it's queued and wait the reserved slot; reject if wait too long.
        if settings.rate_limit_enabled:
            limiter = get_rate_limiter()
            wait = await limiter.reserve(settings.rate_limit_max_queue_wait_seconds)
            if wait < 0:
                logger.warning("Rate limit: request rejected (queue full) [conv=%s]", conv_id)
                await turn_context.send_activity(
                    "🚦 Hệ thống đang quá tải (vượt giới hạn "
                    f"{settings.rate_limit_max_requests} yêu cầu/"
                    f"{settings.rate_limit_window_seconds}s). "
                    "Vui lòng thử lại sau ít phút."
                )
                return
            if wait > 0:
                logger.info("Rate limit: request queued ~%.0fs [conv=%s]", wait, conv_id)
                await turn_context.send_activity(
                    "⏳ Hệ thống đang xử lý nhiều yêu cầu (giới hạn "
                    f"{settings.rate_limit_max_requests}/{settings.rate_limit_window_seconds}s). "
                    f"Yêu cầu của bạn đã được đưa vào hàng đợi, dự kiến chờ ~{wait:.0f}s. "
                    "Vui lòng đợi trong giây lát…"
                )
                await asyncio.sleep(wait)

        has_checkpointer = self._graph.checkpointer is not None
        lg_config = {
            "configurable": {
                "thread_id": conv_id,
                "actor_id": conv_id,
            }
        }

        # Load state: from AgentBase checkpointer (persistent) or in-memory store (fallback)
        if has_checkpointer:
            try:
                snapshot = await self._graph.aget_state(lg_config)
                current_state = dict(snapshot.values) if snapshot and snapshot.values else empty_state()
            except Exception as exc:
                logger.warning("Checkpointer load failed [conv=%s]: %s — using empty state", conv_id, exc)
                current_state = empty_state()
        else:
            current_state = self._store.get(conv_id)

        # Extract Teams user identity on every turn (email may become available later)
        user_identity = await extract_teams_user(turn_context)

        state = {
            **current_state,
            "messages": [*current_state.get("messages", []), {"role": "user", "content": user_text}],
            "current_user": user_identity,
        }

        try:
            result = await self._graph.ainvoke(state, config=lg_config if has_checkpointer else None)
        except Exception as exc:
            logger.error("Graph invocation failed [conv=%s]: %s", conv_id, exc, exc_info=True)
            await turn_context.send_activity("Xin lỗi, đang gặp sự cố. Vui lòng thử lại.")
            return

        # Checkpointer persists automatically; fallback to in-memory store when not set
        if not has_checkpointer:
            self._store.save(conv_id, result)

        # Extract the last assistant message as the reply
        reply = ""
        for msg in reversed(result["messages"]):
            if msg.get("role") == "assistant":
                reply = msg["content"]
                break

        await turn_context.send_activity(reply)

    async def on_members_added_activity(
        self, members_added: List[ChannelAccount], turn_context: TurnContext
    ) -> None:
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(
                    "Xin chào! Tôi là Jira Agent 👋\n"
                    "Hãy mô tả yêu cầu của bạn và tôi sẽ giúp tạo Jira ticket chuẩn production."
                )
