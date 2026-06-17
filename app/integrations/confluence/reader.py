"""Confluence REST API reader — STRICTLY READ-ONLY.

No write, modify, or delete operations are exposed here.
"""
import base64
import html
import logging
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx

from app.config import settings
from app.core.errors import ConfluenceUnavailable
from app.core.retry import with_retry

logger = logging.getLogger(__name__)


def _confluence_effective_base() -> str:
    """Return the effective Confluence base URL (unchanged — server_url is used as-is)."""
    return settings.confluence_server_url.rstrip("/")


def _confluence_transport_kwargs() -> dict:
    """Return httpx.AsyncClient kwargs.

    When CONFLUENCE_HOST_HEADER is set the URL is an IP. Force HTTP/1.1 (http2=False)
    so Nginx reads the Host header for virtual-host routing — same trick as Grafana's
    requests.Session approach.
    """
    if not settings.confluence_host_header:
        return {"verify": True}
    logger.info(
        "confluence: Host header override → %s (HTTP/1.1, SSL verify disabled)",
        settings.confluence_host_header,
    )
    return {"verify": False}


# ── HTML → plain text ─────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Minimal HTML parser that collects visible text only."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list = []
        self._skip_tags = {"script", "style", "head"}
        self._in_skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag.lower() in self._skip_tags:
            self._in_skip += 1
        # Add a space before block-level elements so words don't run together
        if tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._skip_tags:
            self._in_skip = max(0, self._in_skip - 1)

    def handle_data(self, data: str) -> None:
        if self._in_skip == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of whitespace / blank lines
        raw = html.unescape(raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]+", " ", raw)
        return raw.strip()


def _html_to_text(html_content: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        # Fallback: strip tags with regex if parser chokes on malformed HTML
        return re.sub(r"<[^>]+>", " ", html_content).strip()
    return extractor.get_text()


# ── URL → page ID ─────────────────────────────────────────────────────────────

async def extract_page_id_from_url(url: str, base: str) -> str:
    """Parse or resolve the Confluence page ID from *url*.

    Handles the following URL formats:
    - .../pages/viewpage.action?pageId=123456
    - .../wiki/spaces/SPACE/pages/123456[/Title]
    - .../display/SPACE/Page+Title          (title search API)
    - .../x/<key>                            (tiny/share link — resolved via redirect)
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # Format 1: ?pageId=xxx
    if "pageId" in qs:
        return qs["pageId"][0]

    # Format 2: /wiki/spaces/SPACE/pages/<id>[/...] or /pages/<id>
    m = re.search(r"/pages/(\d+)", parsed.path)
    if m:
        return m.group(1)

    # Format 3: tiny/share link /x/<key> — decode locally (Confluence tiny link
    # encodes the page id) and fall back to following the redirect if needed.
    m = re.search(r"/x/([A-Za-z0-9_-]+)", parsed.path)
    if m:
        decoded = _decode_tiny_link(m.group(1))
        if decoded:
            logger.info("confluence: tiny link %r → pageId %s (local decode)", m.group(1), decoded)
            return decoded
        return await _resolve_short_link(url, base)

    # Format 4: /display/SPACE/Page+Title — resolve via title search API
    m = re.search(r"/display/([^/]+)/(.+)$", parsed.path)
    if m:
        space_key = unquote_plus(m.group(1))
        title = unquote_plus(m.group(2))   # decodes %5B → '[', '+' → ' '
        return await _resolve_page_id_by_title(space_key, title, url, base)

    raise ConfluenceUnavailable(url, f"Cannot determine page ID from URL format: {parsed.path!r}")


def _decode_tiny_link(key: str) -> Optional[str]:
    """Decode a Confluence tiny-link key (the part after /x/) into a page id.

    Confluence encodes the page id as little-endian bytes → base64, then makes it
    URL-safe with '/'→'-' and '+'→'_'. We reverse that to recover the id locally —
    no HTTP call, so it sidesteps web-endpoint auth quirks.
    """
    try:
        s = key.replace("-", "/").replace("_", "+")
        s += "=" * (-len(s) % 4)               # restore base64 padding
        raw = base64.b64decode(s)
        page_id = int.from_bytes(raw, byteorder="little")
        return str(page_id) if page_id > 0 else None
    except Exception as exc:
        logger.warning("confluence: failed to decode tiny link %r — %s", key, exc)
        return None


async def _resolve_short_link(url: str, base: str) -> str:
    """Resolve a Confluence tiny/share link (/x/<key>) to a page ID.

    Follows redirects to the canonical page, then extracts the page ID from the
    final URL or from the page HTML (ajs-page-id / data-page-id meta).
    """
    headers, auth = _build_auth_headers()
    try:
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True, **_confluence_transport_kwargs()) as http:
            resp = await (http.get(url, auth=auth) if auth else http.get(url))
    except httpx.RequestError as exc:
        raise ConfluenceUnavailable(url, f"Network error resolving short link: {exc}") from exc

    if resp.status_code == 401:
        raise ConfluenceUnavailable(url, "Auth failed (401) — kiểm tra CONFLUENCE_API_TOKEN")
    if resp.status_code == 403:
        raise ConfluenceUnavailable(url, "Access denied (403)")
    if resp.status_code != 200:
        raise ConfluenceUnavailable(url, f"short link → HTTP {resp.status_code}")

    final = str(resp.url)
    # pageId / /pages/<id> in the resolved URL
    m = re.search(r"pageId=(\d+)", final) or re.search(r"/pages/(\d+)", final)
    if m:
        return m.group(1)
    # page id embedded in the page HTML (Confluence Server pages)
    m = (
        re.search(r'name="ajs-page-id"\s+content="(\d+)"', resp.text)
        or re.search(r'data-page-id="(\d+)"', resp.text)
        or re.search(r'"contentId"\s*:\s*"?(\d+)"?', resp.text)
    )
    if m:
        return m.group(1)
    # last resort: resolved to a /display/ URL → title search
    if "/display/" in final:
        return await extract_page_id_from_url(final, base)
    raise ConfluenceUnavailable(url, "Cannot extract page ID from short-link target")


async def _resolve_page_id_by_title(
    space_key: str, title: str, original_url: str, base: str
) -> str:
    """Resolve a page ID from space key + title.

    1. Exact-title lookup via /rest/api/content?title=...
    2. Fallback to a CQL fuzzy search (title ~ "...") — handles renamed pages,
       casing/spacing differences, and stale display slugs.
    """
    headers, auth = _build_auth_headers()

    async def _get(api_url: str, params: dict):
        try:
            async with httpx.AsyncClient(timeout=15, headers=headers, **_confluence_transport_kwargs()) as http:
                if auth:
                    return await http.get(api_url, params=params, auth=auth)
                return await http.get(api_url, params=params)
        except httpx.RequestError as exc:
            raise ConfluenceUnavailable(original_url, f"Network error: {exc}") from exc

    def _check(resp):
        if resp.status_code == 401:
            raise ConfluenceUnavailable(original_url, "Auth failed (401) — kiểm tra CONFLUENCE_API_TOKEN")
        if resp.status_code == 403:
            raise ConfluenceUnavailable(original_url, "Access denied (403)")

    # 1. Exact title match
    resp = await _get(f"{base}/rest/api/content", {"spaceKey": space_key, "title": title, "limit": 1})
    _check(resp)
    if resp.status_code == 200:
        results = resp.json().get("results", [])
        if results:
            logger.info("confluence: exact-title match for %r → %s", title, results[0]["id"])
            return str(results[0]["id"])

    # 2. CQL fuzzy fallback
    safe_title = title.replace('"', "")
    cql = f'space = "{space_key}" AND title ~ "{safe_title}"'
    logger.info("confluence: exact title miss — trying CQL %s", cql)
    resp = await _get(f"{base}/rest/api/content/search", {"cql": cql, "limit": 5})
    _check(resp)
    if resp.status_code == 200:
        results = resp.json().get("results", [])
        if results:
            # Prefer an exact (case-insensitive) title hit, else the first result
            for r in results:
                if (r.get("title") or "").strip().lower() == title.strip().lower():
                    logger.info("confluence: CQL exact-title hit → %s", r["id"])
                    return str(r["id"])
            logger.info("confluence: CQL best match %r → %s", results[0].get("title"), results[0]["id"])
            return str(results[0]["id"])

    raise ConfluenceUnavailable(
        original_url, f"Page not found: space={space_key} title={title!r}"
    )


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _build_auth_headers():
    """Return (headers_dict, auth_tuple_or_None) based on config.

    API token is resolved from AgentBase at startup (via secrets.get_secret),
    falling back to CONFLUENCE_API_TOKEN in .env.
    """
    from app.core.secrets import get_secret
    token = get_secret("confluence-api-key", settings.confluence_api_token)
    headers = {"Accept": "application/json"}
    if settings.confluence_host_header:
        headers["Host"] = settings.confluence_host_header
    if settings.confluence_user_email:
        return headers, (settings.confluence_user_email, token)
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers, None


# ── Main public function ──────────────────────────────────────────────────────

@with_retry(max_attempts=3, backoff_base=1.0, exceptions=(httpx.RequestError,))
async def fetch_page_content(url: str) -> str:
    """Fetch Confluence page *url* and return its body as plain text.

    Uses the host from *url* itself as the API base — does NOT rely on
    CONFLUENCE_SERVER_URL, which may be misconfigured or left at default localhost.

    Raises:
        ConfluenceUnavailable: if the page cannot be fetched (404, 403, network error).
    """
    parsed = urlparse(url)
    # Derive base from the URL itself so we always call the right server
    base = f"{parsed.scheme}://{parsed.netloc}"

    headers, auth = _build_auth_headers()

    try:
        page_id = await extract_page_id_from_url(url, base)
    except ConfluenceUnavailable:
        raise
    except Exception as exc:
        raise ConfluenceUnavailable(url, f"URL parse error: {exc}") from exc

    params = {"expand": "body.storage,body.view"}

    async def _get_content(pid: str):
        api_url = f"{base}/rest/api/content/{pid}"
        try:
            async with httpx.AsyncClient(timeout=20, headers=headers, **_confluence_transport_kwargs()) as http:
                if auth:
                    return await http.get(api_url, params=params, auth=auth)
                return await http.get(api_url, params=params)
        except httpx.RequestError as exc:
            raise ConfluenceUnavailable(url, f"Network error: {exc}") from exc

    logger.info("confluence: fetching page id=%s from %s", page_id, base)
    resp = await _get_content(page_id)

    # If a tiny-link-decoded id 404s, the decode may be off — fall back to the
    # HTTP redirect resolution and retry once.
    if resp.status_code == 404 and re.search(r"/x/[A-Za-z0-9_-]+", urlparse(url).path):
        logger.info("confluence: id %s 404 for short link — trying redirect resolution", page_id)
        try:
            alt_id = await _resolve_short_link(url, base)
            if alt_id and alt_id != page_id:
                page_id = alt_id
                resp = await _get_content(page_id)
        except ConfluenceUnavailable as exc:
            logger.warning("confluence: short-link fallback failed — %s", exc)

    if resp.status_code == 401:
        raise ConfluenceUnavailable(url, "Auth failed (401) — kiểm tra CONFLUENCE_API_TOKEN trong .env")
    if resp.status_code == 403:
        raise ConfluenceUnavailable(url, "Access denied (403) — token không có quyền đọc page này")
    if resp.status_code == 404:
        raise ConfluenceUnavailable(url, f"Page not found (404) — page_id={page_id}")
    if resp.status_code != 200:
        raise ConfluenceUnavailable(url, f"HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    # Prefer body.storage (raw wiki markup storage format), fallback to body.view (rendered HTML)
    html_body: Optional[str] = (
        data.get("body", {}).get("storage", {}).get("value")
        or data.get("body", {}).get("view", {}).get("value")
    )
    if not html_body:
        logger.warning("confluence: page %s has empty body", page_id)
        return ""

    plain = _html_to_text(html_body)
    logger.info("confluence: fetched page %s (%d chars plain text)", page_id, len(plain))
    return plain


async def check_confluence_auth(test_url: Optional[str] = None) -> dict:
    """Diagnostic: verify Confluence credentials and connectivity.

    Returns a dict with status, server, user info, or error detail.
    """
    base = _confluence_effective_base()
    if test_url:
        parsed = urlparse(test_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

    headers, auth = _build_auth_headers()
    api_url = f"{base}/rest/api/user/current"
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers, **_confluence_transport_kwargs()) as http:
            resp = await (http.get(api_url, auth=auth) if auth else http.get(api_url))
    except httpx.RequestError as exc:
        return {"status": "error", "detail": f"Network error: {exc}", "server": base}

    if resp.status_code == 200:
        data = resp.json()
        return {
            "status": "ok",
            "server": base,
            "user": data.get("displayName", data.get("username", "unknown")),
            "auth_mode": "Basic" if settings.confluence_user_email else "Bearer PAT",
        }
    return {
        "status": "error",
        "server": base,
        "http_status": resp.status_code,
        "detail": resp.text[:300],
    }
