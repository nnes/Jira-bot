"""Reranker node — enrich Confluence content using qwen/qwen3-reranker-8b.

Called only when state["confluence_url"] is set.
Chunks the raw page content, scores each chunk against the conversation
context, and keeps the top-K most relevant chunks.
"""
import asyncio
import logging
import re
from typing import Any, Dict, List

from app.graph.state import AgentState
from app.llm.client import get_llm_client
from app.llm.registry import ModelRole, get_model

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 3000          # chars per chunk (≈ 700–900 tokens) — large enough to keep full sections
_TOP_K = 5                  # number of top chunks to keep
_MAX_CONTENT_CHARS = 80_000  # hard limit before chunking — skip extremely long boilerplate pages
_MAX_OUTPUT_CHARS = 15_000   # cap on total chars sent to generator


# ── Chunking ──────────────────────────────────────────────────────────────────

def _split_into_chunks(text: str, chunk_size: int = _CHUNK_SIZE) -> List[str]:
    """Split *text* into chunks of at most *chunk_size* chars, preferring
    paragraph/heading boundaries to avoid cutting mid-sentence.
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    # Split on headings (##, ===, ---) or blank lines
    paragraphs = re.split(r"\n{2,}|(?=^#{1,6} )", text, flags=re.MULTILINE)
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 1 <= chunk_size:
            current = (current + "\n\n" + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            # If the paragraph itself exceeds chunk_size, hard-split it
            while len(para) > chunk_size:
                chunks.append(para[:chunk_size])
                para = para[chunk_size:]
            current = para

    if current:
        chunks.append(current)

    return chunks


# ── Scoring ───────────────────────────────────────────────────────────────────

def _parse_score(content: str) -> float:
    """Extract a relevance score (0.0–10.0) from the model's raw output.

    The qwen3-reranker-8b model may output:
    - A float like "7.3" or "8"
    - "yes" / "true" → treated as 8.0
    - "no" / "false" → treated as 2.0
    Falls back to 5.0 when unable to parse.
    """
    content = content.strip().lower()
    if content in ("yes", "true"):
        return 8.0
    if content in ("no", "false"):
        return 2.0
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", content)
    if m:
        try:
            score = float(m.group(1))
            # Normalise: if the model responds in 0–1 range, scale to 0–10
            if score <= 1.0:
                score *= 10.0
            return min(score, 10.0)
        except ValueError:
            pass
    return 5.0


async def _score_chunk(client: Any, model: str, query: str, chunk: str) -> float:
    """Score a single chunk against *query* using the reranker model."""
    prompt = (
        f"<query>{query}</query>\n"
        f"<doc>{chunk}</doc>\n\n"
        "Rate the relevance of the document to the query on a scale from 0 to 10. "
        "Output a single number only."
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8,
            temperature=0.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        return _parse_score(raw)
    except Exception as exc:
        logger.warning("reranker: scoring failed for chunk — %s", exc)
        return 5.0


# ── Node ──────────────────────────────────────────────────────────────────────

def _build_rerank_query(state: AgentState) -> str:
    """Build a rich reranking query from collected slots + recent user messages.

    Slots (feature description, system name, etc.) give the reranker more signal
    than raw conversation history alone.
    """
    parts: List[str] = []

    slots = state.get("slots") or {}
    slot_fields = [
        slots.get("feature_description", ""),
        slots.get("system_name", ""),
        slots.get("requirement_type", ""),
        slots.get("summary", ""),
    ]
    slot_text = " ".join(f for f in slot_fields if f).strip()
    if slot_text:
        parts.append(slot_text)

    messages = state.get("messages", [])
    user_msgs = [m["content"] for m in messages[-8:] if m.get("role") == "user"]
    if user_msgs:
        parts.append(" ".join(user_msgs)[-600:])

    return " ".join(parts)[-1000:] or "Jira ticket fintech requirements"


async def reranker_node(state: AgentState) -> Dict[str, Any]:
    """Rerank Confluence page content and update state["confluence_data"]."""
    raw_content: str = state.get("confluence_data") or ""
    confluence_url: str = state.get("confluence_url") or ""

    if not raw_content:
        logger.info("reranker: no confluence_data to rerank — skipping")
        return {**state}

    # Hard-truncate absurdly long pages before chunking (boilerplate heavy docs)
    if len(raw_content) > _MAX_CONTENT_CHARS:
        logger.warning(
            "reranker: page too large (%d chars) — truncating to %d before chunking",
            len(raw_content), _MAX_CONTENT_CHARS,
        )
        raw_content = raw_content[:_MAX_CONTENT_CHARS]

    client = get_llm_client()
    model = get_model(ModelRole.RERANKER)

    query = _build_rerank_query(state)
    logger.debug("reranker: query = %r", query[:200])

    chunks = _split_into_chunks(raw_content)
    if not chunks:
        return {**state}

    logger.info(
        "reranker: scoring %d chunks from %s with model %s (parallel)",
        len(chunks), confluence_url, model,
    )

    # Score all chunks in parallel for speed
    scores: List[float] = await asyncio.gather(
        *[_score_chunk(client, model, query, chunk) for chunk in chunks]
    )

    scored: List[tuple] = [
        (scores[i], i, chunks[i]) for i in range(len(chunks))
    ]
    for score, idx, _ in scored:
        logger.debug("reranker: chunk %d score=%.1f", idx + 1, score)

    # Select top-K chunks, then restore original document order (preserves narrative flow)
    scored.sort(key=lambda x: x[0], reverse=True)
    top_indices = sorted(idx for _, idx, _ in scored[:_TOP_K])
    top_chunks = [chunks[i] for i in top_indices]

    enriched = "\n\n---\n\n".join(top_chunks)

    # Cap total output so generator context stays within model limits
    if len(enriched) > _MAX_OUTPUT_CHARS:
        enriched = enriched[:_MAX_OUTPUT_CHARS]
        logger.warning("reranker: output capped at %d chars", _MAX_OUTPUT_CHARS)

    logger.info(
        "reranker: kept top-%d chunks in doc order (%d → %d chars)",
        _TOP_K, len(state.get("confluence_data") or ""), len(enriched),
    )

    return {**state, "confluence_data": enriched}
