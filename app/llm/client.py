from typing import Optional

from openai import AsyncOpenAI

from app.config import settings

_client: Optional[AsyncOpenAI] = None


def get_llm_client() -> AsyncOpenAI:
    """Return a module-level singleton AsyncOpenAI-compatible client.

    Reuses the same HTTP connection pool across all calls in the process.
    API key is resolved from AgentBase at startup (via secrets.get_secret),
    falling back to LLM_API_KEY in .env.
    """
    global _client
    if _client is None:
        from app.core.secrets import get_secret
        _client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=get_secret("llm-api-key", settings.llm_api_key),
        )
    return _client


def reset_llm_client() -> None:
    """Force singleton rebuild on next get_llm_client() call."""
    global _client
    _client = None
