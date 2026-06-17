import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.logging import setup_logging
from app.core.dns import apply_dns_overrides

setup_logging()
apply_dns_overrides(settings.dns_overrides)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.secrets import resolve_all_secrets
    await resolve_all_secrets()
    yield


# Disable Swagger / ReDoc / OpenAPI schema in production (set ENABLE_DOCS=true locally).
_docs_kwargs = (
    {}
    if settings.enable_docs
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)
app = FastAPI(
    title="Multi-Model Jira Agent",
    debug=settings.debug,
    lifespan=lifespan,
    **_docs_kwargs,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Diagnostic endpoints — registered ONLY when ENABLE_DIAGNOSTICS=true ────────
# These expose internal info (Jira user/email) and an SSRF surface (Confluence
# fetch-by-URL), so they must stay OFF in production.
if settings.enable_diagnostics:

    @app.get("/api/jira/check")
    async def jira_check() -> dict:
        """Diagnostic endpoint: verify Jira credentials without creating anything."""
        if settings.use_mock_jira:
            return {"status": "mock", "message": "USE_MOCK_JIRA=true — Jira not connected"}
        from app.core.errors import JiraError
        from app.integrations.jira.client import get_jira_client
        try:
            jira = get_jira_client()
            user = await jira.check_auth()
            return {
                "status": "ok",
                "user": user.get("displayName", "unknown"),
                "email": user.get("emailAddress", ""),
                "server": settings.jira_server_url,
            }
        except JiraError as exc:
            return {"status": "error", "detail": str(exc)}

    @app.get("/api/confluence/check")
    async def confluence_check(url: str = "") -> dict:
        """Diagnostic: verify Confluence credentials and optionally fetch a page."""
        from urllib.parse import urlparse
        from app.integrations.confluence.reader import (
            check_confluence_auth,
            extract_page_id_from_url,
            fetch_page_content,
        )
        from app.core.errors import ConfluenceUnavailable

        result = await check_confluence_auth(test_url=url or None)
        if url and result.get("status") == "ok":
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            # Show how the URL resolves to a page id (helps debug tiny/display links)
            try:
                result["resolved_page_id"] = await extract_page_id_from_url(url, base)
            except ConfluenceUnavailable as exc:
                result["resolve_error"] = str(exc)
            try:
                content = await fetch_page_content(url)
                result["page_chars"] = len(content)
                result["page_preview"] = content[:200]
            except ConfluenceUnavailable as exc:
                result["page_error"] = str(exc)
        return result


# ── Local bot endpoint — registered ONLY when ENABLE_MESSAGES_ENDPOINT=true ────
# Local/Emulator dev uses this. In production, Azure Bot Service hosts the bot, so
# this unauthenticated-by-default endpoint is NOT exposed.
if settings.enable_messages_endpoint:
    from botbuilder.schema import Activity
    from app.bot.adapter import adapter
    from app.bot.handler import OrchestratorHandler

    _bot = OrchestratorHandler()

    @app.post("/api/messages")
    async def messages(request: Request) -> Response:
        try:
            body = await request.json()
            activity = Activity().deserialize(body)
            auth_header = request.headers.get("Authorization", "")
            invoke_response = await adapter.process_activity(activity, auth_header, _bot.on_turn)
        except Exception as exc:
            logger.error("Error processing activity: %s", exc, exc_info=True)
            return Response(status_code=500)

        if invoke_response:
            return JSONResponse(content=invoke_response.body, status_code=invoke_response.status)
        return Response(status_code=201)

    logger.info("Local bot endpoint /api/messages ENABLED")
else:
    logger.info("Local bot endpoint /api/messages DISABLED (production / Azure Bot Service mode)")
