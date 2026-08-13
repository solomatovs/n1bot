"""Ручной прогон операций Confluence: ConfluenceOps вызывается напрямую.

Профиль и адрес берутся из [tool.confluence]; запросы идут в сеть из процесса
теста, аргументы вызова задаются в RunArgs.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from boba.settings import bind
from boba.tool.kb.confluence import ConfluenceToolsConfig
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.payload import ConfluenceOps
from boba.tool.kb.confluence.protocol import (
    ConfluenceGrepRequest,
    ConfluencePageRequest,
    ConfluenceSearchRequest,
    ConfluenceSpacesRequest,
)

pytestmark = [pytest.mark.run, pytest.mark.anyio]


class RunArgs:
    """Аргументы прогона: правятся перед запуском."""

    PAGE_ID: ClassVar[str] = "950276"

    PATTERN: ClassVar[str] = "доступ"

    CQL: ClassVar[str] = "type = page order by lastmodified desc"

    SPACE_TYPE: ClassVar[str] = "global"

    AS_MARKDOWN: ClassVar[bool] = True

    CASE_INSENSITIVE: ClassVar[bool] = True

    CONTEXT: ClassVar[int] = 2

    LIMIT: ClassVar[int] = 20

    FIXED_STRING: ClassVar[bool] = True

    SNIPPET_CHARS: ClassVar[int] = 500


@pytest.fixture(scope="module")
def confluence_config(raw_config):
    return bind(raw_config, path="tool.confluence", model=ConfluenceToolsConfig)


@pytest.fixture(scope="module")
def confluence_url(confluence_config) -> str:
    return confluence_config.confluence.base_url or ""


@pytest.fixture(scope="module")
def confluence_profile(confluence_config):
    return ConfluenceCaller.transport_of(confluence_config.confluence)


async def test_run_confluence_page(
    confluence_config, confluence_url, confluence_profile, payload
) -> None:
    request = ConfluencePageRequest(
        op=ConfluencePageRequest.OP,
        page_id=RunArgs.PAGE_ID,
        body_format=confluence_config.body_format,
        as_markdown=RunArgs.AS_MARKDOWN,
        base_url=confluence_url,
        profile=confluence_profile,
    )

    answer = await ConfluenceOps.page(payload.of(request))

    print(answer)


async def test_run_confluence_grep(
    confluence_config, confluence_url, confluence_profile, payload, chunks
) -> None:
    request = ConfluenceGrepRequest(
        op=ConfluenceGrepRequest.OP,
        page_id=RunArgs.PAGE_ID,
        body_format=confluence_config.body_format,
        as_markdown=RunArgs.AS_MARKDOWN,
        pattern=RunArgs.PATTERN,
        case_insensitive=RunArgs.CASE_INSENSITIVE,
        context=RunArgs.CONTEXT,
        limit=RunArgs.LIMIT,
        fixed_string=RunArgs.FIXED_STRING,
        max_text_chars=confluence_config.max_text_chars,
        base_url=confluence_url,
        profile=confluence_profile,
    )

    await ConfluenceOps.grep(payload.of(request), chunks.write)

    print(chunks.rows())


async def test_run_confluence_search(
    confluence_url, confluence_profile, payload, chunks
) -> None:
    request = ConfluenceSearchRequest(
        op=ConfluenceSearchRequest.OP,
        cql=RunArgs.CQL,
        limit=RunArgs.LIMIT,
        snippet_chars=RunArgs.SNIPPET_CHARS,
        base_url=confluence_url,
        profile=confluence_profile,
    )

    await ConfluenceOps.search(payload.of(request), chunks.write)

    print(chunks.rows())


async def test_run_confluence_spaces(
    confluence_url, confluence_profile, payload, chunks
) -> None:
    request = ConfluenceSpacesRequest(
        op=ConfluenceSpacesRequest.OP,
        space_type=RunArgs.SPACE_TYPE,
        limit=RunArgs.LIMIT,
        base_url=confluence_url,
        profile=confluence_profile,
    )

    await ConfluenceOps.spaces(payload.of(request), chunks.write)

    print(chunks.rows())
