"""Resolve API secrets from AgentBase Identity Service at startup.

Keys are fetched once and cached in `_resolved`. All factory functions call
`get_secret(provider, fallback)` which returns the cached value or falls back
to the .env value — so AgentBase being unavailable never crashes the app.
"""
import logging
from typing import Dict

from app.config import settings

logger = logging.getLogger(__name__)

_resolved: Dict[str, str] = {}


def get_secret(provider: str, fallback: str = "") -> str:
    """Return AgentBase-resolved secret or .env fallback."""
    return _resolved.get(provider) or fallback


async def resolve_all_secrets() -> None:
    """Fetch all API keys from AgentBase at startup and cache them in-process.

    Runs inside FastAPI lifespan — failures are non-fatal and logged as warnings
    so the app can still boot using .env values.
    """
    identity = settings.agent_identity_name
    if not identity:
        logger.info("secrets: AGENT_IDENTITY_NAME not set — skipping AgentBase resolution")
        return

    try:
        from greennode_agentbase import IdentityClient, IAMCredentials
        client = IdentityClient(iam_credentials=IAMCredentials())
    except Exception as exc:
        logger.warning("secrets: cannot create IdentityClient (%s) — using .env fallbacks", exc)
        return

    for provider in ("llm-api-key", "jira-api-key", "confluence-api-key"):
        try:
            result = await client.get_api_key_for_agent_identity_async(
                provider_name=provider,
                agent_identity_name=identity,
            )
            _resolved[provider] = result.apikey
            logger.info("secrets: resolved %s from AgentBase", provider)
        except Exception as exc:
            logger.warning(
                "secrets: could not resolve %s (%s) — using .env fallback", provider, exc
            )
