"""Tests for Confluence URL → page ID extraction (pageId, /pages, /display, /x short link)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors import ConfluenceUnavailable
from app.integrations.confluence import reader

BASE = "https://confluence.zalopay.vn"


def _cm(fake_http):
    """Wrap an AsyncMock http client as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=fake_http)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_pageid_query():
    pid = await reader.extract_page_id_from_url(
        BASE + "/pages/viewpage.action?pageId=123456", BASE
    )
    assert pid == "123456"


@pytest.mark.asyncio
async def test_pages_path():
    pid = await reader.extract_page_id_from_url(BASE + "/spaces/ZTM/pages/789/Some-Title", BASE)
    assert pid == "789"


@pytest.mark.asyncio
async def test_short_link_local_decode_no_http():
    """Tiny links decode to the page id locally — no HTTP call, no auth issues."""
    # If AsyncClient were used it would raise, proving we never hit the network.
    boom = MagicMock(side_effect=AssertionError("should not make an HTTP call"))
    with patch.object(reader.httpx, "AsyncClient", boom):
        pid = await reader.extract_page_id_from_url(BASE + "/x/PHaWDw", BASE)
    assert pid == "261518908"


@pytest.mark.asyncio
async def test_short_link_http_fallback_when_decode_fails():
    resp = MagicMock()
    resp.status_code = 200
    resp.url = BASE + "/pages/viewpage.action?pageId=999111"
    resp.text = ""
    http = AsyncMock(); http.get.return_value = resp
    with patch.object(reader, "_decode_tiny_link", return_value=None), \
         patch.object(reader.httpx, "AsyncClient", return_value=_cm(http)):
        pid = await reader.extract_page_id_from_url(BASE + "/x/PHaWDw", BASE)
    assert pid == "999111"


@pytest.mark.asyncio
async def test_short_link_http_fallback_resolves_via_html_meta():
    resp = MagicMock()
    resp.status_code = 200
    resp.url = BASE + "/display/ZTM/Some+Page"
    resp.text = '<html><meta name="ajs-page-id" content="555"></html>'
    http = AsyncMock(); http.get.return_value = resp
    with patch.object(reader, "_decode_tiny_link", return_value=None), \
         patch.object(reader.httpx, "AsyncClient", return_value=_cm(http)):
        pid = await reader.extract_page_id_from_url(BASE + "/x/abc123", BASE)
    assert pid == "555"


@pytest.mark.asyncio
async def test_short_link_http_fallback_403_raises():
    resp = MagicMock(); resp.status_code = 403; resp.text = ""
    http = AsyncMock(); http.get.return_value = resp
    with patch.object(reader, "_decode_tiny_link", return_value=None), \
         patch.object(reader.httpx, "AsyncClient", return_value=_cm(http)):
        with pytest.raises(ConfluenceUnavailable):
            await reader.extract_page_id_from_url(BASE + "/x/abc123", BASE)


@pytest.mark.asyncio
async def test_display_url_cql_fallback_when_exact_miss():
    """When exact-title lookup returns nothing, fall back to CQL fuzzy search."""
    exact = MagicMock(); exact.status_code = 200; exact.json.return_value = {"results": []}; exact.text = ""
    cql = MagicMock(); cql.status_code = 200
    cql.json.return_value = {"results": [{"id": "707070", "title": "[PCF-BANK] VNEID"}]}
    cql.text = ""
    http = AsyncMock(); http.get.side_effect = [exact, cql]
    with patch.object(reader.httpx, "AsyncClient", return_value=_cm(http)):
        pid = await reader.extract_page_id_from_url(BASE + "/display/ZTM/%5BPCF-BANK%5D+VNEID", BASE)
    assert pid == "707070"


@pytest.mark.asyncio
async def test_display_url_decodes_title_before_search():
    captured = {}
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": [{"id": "424242"}]}
    resp.text = ""

    async def fake_get(url, params=None, auth=None):
        captured["params"] = params
        return resp

    http = AsyncMock(); http.get.side_effect = fake_get
    with patch.object(reader.httpx, "AsyncClient", return_value=_cm(http)):
        pid = await reader.extract_page_id_from_url(
            BASE + "/display/ZTM/%5BPCF-BANK%5D+NFC", BASE
        )
    assert pid == "424242"
    # %5B → '[', %5D → ']', '+' → ' '
    assert captured["params"]["title"] == "[PCF-BANK] NFC"
    assert captured["params"]["spaceKey"] == "ZTM"


@pytest.mark.asyncio
async def test_unknown_format_raises():
    with pytest.raises(ConfluenceUnavailable):
        await reader.extract_page_id_from_url(BASE + "/random/path", BASE)
