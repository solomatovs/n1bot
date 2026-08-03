"""Tool confluence_search_cql: полнотекстовый поиск страниц.

Запрос к REST и разбор выдачи делает payload в песочнице; здесь остаётся
сборка CQL и таблица для LLM.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.settings import LLMStringList
from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.protocol import ConfluenceSearchRequest
from boba.toolkit.result import TableResult
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceSearchCqlConfig", "confluence_search_cql"]


class ConfluenceSearchCqlConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_search_cql.

    Config-секция: [tool.kb] (читает только confluence). limit/
    snippet_chars — tool-аргументы LLM.
    """

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile


class CqlSearch:
    """Сборка CQL-запроса; разбор выдачи делает payload."""

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


def confluence_search_cql(  # noqa: PLR0913 — фильтры поиска независимы
    cfg: ConfluenceSearchCqlConfig,
    caller: ConfluenceCaller,
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
        Field(ge=1, description="Максимум hits в ответе."),
    ] = 20,
    snippet_chars: Annotated[
        int,
        Field(ge=1, description=CqlSearch.SNIPPET_DESC),
    ] = CqlSearch.SNIPPET_DEFAULT,
) -> TableResult:
    """Полнотекстовый поиск страниц Confluence (online CQL).

    Возвращает TableResult — таблицу hits с колонками page_id/title/
    space_key/url/snippet/last_modified/version. version — текущая
    confluence-версия страницы: сверь её с `version` из kb-поиска по индексу,
    чтобы понять, что в индексе устарело и страницу нужно переиндексировать.
    """
    request = ConfluenceSearchRequest(
        op=ConfluenceSearchRequest.OP,
        base_url=cfg.confluence.base_url or "",
        profile=ConfluenceCaller.transport_of(cfg.confluence),
        cql=CqlSearch.build_cql(query=query, spaces=spaces),
        limit=limit,
        snippet_chars=snippet_chars,
    )
    rows: list[dict[str, Any]] = []
    for hit in caller.search(request).hits:
        rows.append(hit.model_dump())
    note = "ничего не найдено"
    if rows:
        note = f"найдено: {len(rows)}"
    return TableResult(rows=rows, note=note)
