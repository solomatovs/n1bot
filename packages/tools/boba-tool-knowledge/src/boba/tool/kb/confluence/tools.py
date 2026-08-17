"""Чтение Confluence: функции уровня модуля, модуль — обычная программа.

REST-запросы, пагинация и разбор HTML исполняются в теле — потому оно живёт
в песочнице: наружу не уезжает ни сырой ответ, ни исходная разметка.

Ошибки:
ConfluenceRequestError — REST недоступен или ответил статусом.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal

import httpx
from langchain_core.tools import InjectedToolArg, tool
from pydantic import ConfigDict, Field

from boba.text.grep import GrepLimits, TextGrep
from boba.tool.kb.confluence.parsing import ConfluenceJson
from boba.tool.kb.confluence.request_sources import ConfluenceRest
from boba.toolkit.entry import ToolMain
from boba.toolkit.result import TableResult, TextResult, ToolResult, pack_result
from boba.toolkit.types import LLMStringList, SecretRevealing
from boba.transport.http import HttpProfile

_PAGE_ID_DESCRIPTION = "ID страницы Confluence (из URL `viewpage.action?pageId=<id>`)."


class ConfluenceRequestError(Exception):
    """REST недоступен или ответил статусом; текст готов для пользователя."""


class ConfluenceErrorKind(StrEnum):
    """Ожидаемые отказы confluence-инструментов."""

    REQUEST_FAILED = "confluence_request_failed"


class ConfluenceToolsConfig(SecretRevealing):
    """Конфиг инструментов чтения Confluence; секция [tool.confluence]."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.confluence"

    confluence: HttpProfile = Field(
        description='Web-профиль Confluence ссылкой `confluence = "${web.<name>}"`.',
    )
    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description="Confluence body-формат: view/export_view/storage.",
    )
    max_text_chars: int = Field(
        default=2000,
        ge=1,
        description="Потолок длины content/before/after на match в grep.",
    )


class ConfluenceHttp:
    """Пути и запросы REST Confluence."""

    @staticmethod
    async def get(cfg: ConfluenceToolsConfig, path: str) -> bytes:
        profile = cfg.confluence
        url = (profile.base_url or "").rstrip("/") + path
        try:
            async with httpx.AsyncClient(
                timeout=profile.timeout_sec,
                verify=profile.ssl_verify,
                follow_redirects=True,
                auth=profile.auth.httpx_auth(),
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return await response.aread()
        except httpx.HTTPError as exc:
            msg = f"Confluence request failed: {type(exc).__name__}: {exc}"
            raise ConfluenceRequestError(msg) from exc

    @classmethod
    async def page_json(
        cls, cfg: ConfluenceToolsConfig, page_id: str
    ) -> dict[str, Any]:
        path = ConfluenceRest.page_fetch_path(page_id, body_format=cfg.body_format)
        return json.loads(await cls.get(cfg, path))


class ConfluencePageText:
    """Контент страницы: HTML нужного формата и конверсия в markdown."""

    HEADING_STYLE: ClassVar[str] = "ATX"

    @classmethod
    async def of_page(
        cls, cfg: ConfluenceToolsConfig, page_id: str, *, as_markdown: bool
    ) -> str:
        data = await ConfluenceHttp.page_json(cfg, page_id)
        html = ConfluenceJson.body_html(data, cfg.body_format)
        if not as_markdown:
            return html

        from boba.tool.kb.html.payload import PageOps  # noqa: PLC0415

        answer = PageOps.to_markdown({"html": html, "heading_style": cls.HEADING_STYLE})
        return str(answer["markdown"])

    @staticmethod
    def excerpt_of(html: str, snippet_chars: int) -> str:
        from boba.tool.kb.html.payload import PageOps  # noqa: PLC0415

        excerpt = ""
        if html:
            excerpt = str(PageOps.plain_text({"html": html})["text"])
        if len(excerpt) > snippet_chars:
            excerpt = excerpt[: snippet_chars - 1].rstrip() + "…"
        return excerpt


class CqlSearch:
    """Сборка CQL-запроса и разбор выдачи поиска."""

    SNIPPET_DEFAULT: ClassVar[int] = 1000
    SNIPPET_DESC: ClassVar[str] = (
        "Максимальная длина сниппета на каждый hit (символов). По умолчанию 1000."
    )

    @staticmethod
    def cql_literal(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def build_cql(query: str, spaces: list[str] | None) -> str:
        text_block = f"text ~ {CqlSearch.cql_literal(query)}"
        if not spaces:
            return text_block
        if len(spaces) == 1:
            space_block = f"space = {CqlSearch.cql_literal(spaces[0])}"
        else:
            joined = ", ".join(CqlSearch.cql_literal(s) for s in spaces)
            space_block = f"space in ({joined})"
        return f"({text_block}) and ({space_block})"

    @staticmethod
    def hit_row(hit: dict[str, Any], base: str, snippet_chars: int) -> dict[str, Any]:
        html = ConfluenceJson.body_html(hit, "view")
        excerpt = ConfluencePageText.excerpt_of(html, snippet_chars)

        space = hit.get("space")
        space_key = ""
        if isinstance(space, dict):
            space_key = str(space.get("key") or "")

        webui = str(hit.get("_links", {}).get("webui") or "")

        return {
            "page_id": str(hit.get("id") or ""),
            "title": str(hit.get("title") or ""),
            "space_key": space_key,
            "url": f"{base}{webui}" if webui else base,
            "excerpt": excerpt,
        }


@tool(response_format="content_and_artifact")
async def confluence_fetch(
    page_id: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "ID страницы Confluence (из URL `viewpage.action?pageId=<id>`). "
                "Attachment'ы не скачиваются."
            ),
        ),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Если true — конвертирует HTML в Markdown. "
                "Иначе возвращает исходный Confluence-HTML."
            ),
        ),
    ] = True,
    *,
    cfg: Annotated[ConfluenceToolsConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Скачивает одну Confluence-страницу и возвращает её контент."""
    text = await ConfluencePageText.of_page(cfg, page_id, as_markdown=as_markdown)

    artifact = TextResult(text=text)
    return pack_result(artifact)


@tool(response_format="content_and_artifact")
async def confluence_grep(  # noqa: PLR0913 — независимые флаги grep'а
    page_id: Annotated[
        str,
        Field(min_length=1, description=_PAGE_ID_DESCRIPTION),
    ],
    pattern: Annotated[
        str,
        Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
    ],
    as_markdown: Annotated[
        bool,
        Field(
            description=(
                "Искать по Markdown-конверсии вместо исходного "
                "Confluence-HTML. По умолчанию true."
            ),
        ),
    ] = True,
    case_insensitive: Annotated[
        bool,
        Field(description="Игнорировать регистр. По умолчанию false."),
    ] = False,
    context: Annotated[
        int,
        Field(ge=0, description="Строк контекста до и после каждого совпадения."),
    ] = 0,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум совпадений. По умолчанию 100."),
    ] = 100,
    fixed_string: Annotated[
        bool,
        Field(description="Литеральный поиск без regex. По умолчанию false."),
    ] = False,
    *,
    cfg: Annotated[ConfluenceToolsConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Ищет совпадения по тексту одной Confluence-страницы."""
    text = await ConfluencePageText.of_page(cfg, page_id, as_markdown=as_markdown)

    compiled = TextGrep.compile_pattern(
        pattern, fixed_string=fixed_string, case_insensitive=case_insensitive
    )

    limits = GrepLimits(context=context, limit=limit, clip_chars=cfg.max_text_chars)
    rows, note = TextGrep.matched_rows(text, compiled, limits, f"page_id={page_id}")

    table = TableResult(rows=rows, note=note, metadata={"page_id": page_id})
    return pack_result(table)


@tool(response_format="content_and_artifact")
async def confluence_search(
    query: Annotated[
        str,
        Field(min_length=1, description="Строка полнотекстового поиска в Confluence."),
    ],
    spaces: Annotated[
        LLMStringList | None,
        Field(
            description=(
                "Ограничение поиска по space-ключам Confluence. "
                "Не передавай (или `null`) — поиск по всем space'ам."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум найденных страниц."),
    ] = 20,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=CqlSearch.SNIPPET_DESC),
    ] = CqlSearch.SNIPPET_DEFAULT,
    *,
    cfg: Annotated[ConfluenceToolsConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Ищет страницы в Confluence через CQL и возвращает таблицу hits."""
    cql = CqlSearch.build_cql(query=query, spaces=spaces)
    path = ConfluenceRest.cql_search_path(
        cql, limit=limit, expand="body.view,version,space"
    )

    data = json.loads(await ConfluenceHttp.get(cfg, path))
    base = ConfluenceJson.response_base(data)
    if not base:
        base = str(cfg.confluence.base_url or "").rstrip("/")

    rows: list[dict[str, Any]] = []
    for hit in data.get("results") or []:
        rows.append(CqlSearch.hit_row(hit, base, snippet_chars))

    note = f"found: {len(rows)}"
    if not rows:
        note = "nothing found"

    table = TableResult(rows=rows, note=note)
    return pack_result(table)


@tool(response_format="content_and_artifact")
async def confluence_spaces(
    pattern: Annotated[
        str | None,
        Field(
            description=(
                "Glob-шаблон (регистронезависимо) для key/name спейса. "
                "Совпадение по полю целиком: `*data*` — содержит data."
            ),
        ),
    ] = None,
    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(description="Тип space: global / personal / any."),
    ] = "global",
    limit: Annotated[
        int,
        Field(ge=1, le=1000, description="Максимум спейсов в ответе."),
    ] = 200,
    *,
    cfg: Annotated[ConfluenceToolsConfig, InjectedToolArg],
) -> tuple[str, ToolResult]:
    """Список spaces Confluence с опциональным glob-фильтром."""
    path = ConfluenceRest.space_list_path(space_type, limit=limit)
    data = json.loads(await ConfluenceHttp.get(cfg, path))

    rows: list[dict[str, Any]] = []
    for space in data.get("results") or []:
        row = {
            "key": str(space.get("key") or ""),
            "name": str(space.get("name") or ""),
            "type": str(space.get("type") or ""),
        }

        if pattern is not None:
            lowered = pattern.lower()
            key_match = fnmatch.fnmatch(row["key"].lower(), lowered)
            name_match = fnmatch.fnmatch(row["name"].lower(), lowered)
            if not key_match and not name_match:
                continue

        rows.append(row)

    table = TableResult(rows=rows)
    return pack_result(table)


EXPECTED: Mapping[type[Exception], ConfluenceErrorKind] = {
    ConfluenceRequestError: ConfluenceErrorKind.REQUEST_FAILED,
}

TOOLS: Final = ToolMain.toolset(
    confluence_fetch,
    confluence_grep,
    confluence_search,
    confluence_spaces,
)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
