"""Tool confluence_search_cql: полнотекстовый поиск страниц (REST — в песочнице)."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar

from pydantic import Field

from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.toolkit.result import TableResult
from boba.toolkit.types import LLMStringList

__all__ = ["confluence_search_cql"]


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


def confluence_search_cql(
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
    """Полнотекстовый поиск страниц Confluence (online CQL)."""
    hits = caller.search(
        cql=CqlSearch.build_cql(query=query, spaces=spaces),
        limit=limit,
        snippet_chars=snippet_chars,
    )

    rows: list[dict[str, Any]] = []
    for hit in hits:
        rows.append(hit.model_dump())

    note = "nothing found"
    if rows:
        note = f"found: {len(rows)}"

    return TableResult(rows=rows, note=note)
