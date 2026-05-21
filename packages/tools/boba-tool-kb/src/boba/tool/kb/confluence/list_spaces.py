"""Tool `confluence_list_spaces`: список spaces на Confluence-сервере.

LLM-callable read-only tool: возвращает markdown-таблицу спейсов,
доступных текущей роли (anonymous/PAT/basic — по `[tool.kb.confluence]`).
Используется перед `confluence_space_ingest`, чтобы LLM мог увидеть,
какие space-ключи существуют, и выбрать релевантные.

Endpoint: `GET /rest/api/space` с query-параметрами `limit=N&start=0`,
опциональным `&type=global|personal` и `&expand=description.plain` —
cursor-based пагинация через `_links.next`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from boba.tool.kb._markdown import format_markdown_table
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources._common import (
    iter_paginated,
    make_discovery_client,
)
from boba.tools import FromConfig, tool

__all__ = ["confluence_list_spaces"]

_PAGE_LIMIT = 50
_MAX_CELL_CHARS = 200


@tool
def confluence_list_spaces(
    conn_cfg: Annotated[ConfluenceConnectionConfig, FromConfig()],
    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(
            description=(
                "Фильтр по типу space'а: `global` (командные), `personal` "
                "(личные user'ов), `any` (без фильтра, оба типа). По "
                "умолчанию `global` — обычно LLM нужны только командные."
            ),
        ),
    ] = "global",
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=1000,
            description=(
                "Hard cap на количество возвращаемых spaces (защита от "
                "огромного payload'а на больших Confluence-серверах). "
                "Если spaces больше — таблица будет помечена как truncated."
            ),
        ),
    ] = 200,
) -> dict[str, Any]:
    """Список spaces, доступных роли DSN-а (anonymous/PAT/basic).

    Возвращает markdown с колонками `key, name, type, description`. LLM
    использует это, чтобы решить, какой space передать в
    `confluence_space_ingest` или `confluence_space_download`.
    """
    auth = ConfluenceConnection.make_auth(conn_cfg)

    # `?expand=description.plain` — короткое описание; пустое для большинства
    # space'ов, но иногда заполнено. `type=` фильтр серверный.
    type_filter = "" if space_type == "any" else f"&type={space_type}"
    path = (
        f"/rest/api/space?limit={_PAGE_LIMIT}&start=0"
        f"{type_filter}"
        f"&expand=description.plain"
    )

    rows: list[tuple[Any, ...]] = []
    truncated = False
    with make_discovery_client(
        conn_cfg.base_url, auth, conn_cfg.timeout_sec,
    ) as client:
        for raw in iter_paginated(client, path):
            if len(rows) >= limit:
                truncated = True
                break
            rows.append((
                str(raw.get("key") or "").strip(),
                str(raw.get("name") or "").strip(),
                str(raw.get("type") or "").strip(),
                _extract_description(raw),
            ))

    table_md = format_markdown_table(
        columns=["key", "name", "type", "description"],
        rows=rows,
        max_cell_chars=_MAX_CELL_CHARS,
        truncated=truncated,
        truncated_msg=f"more spaces omitted (увеличьте limit, текущий {limit})",
    )
    return {
        "table": table_md,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _extract_description(raw: dict[str, Any]) -> str:
    desc = raw.get("description") or {}
    plain = desc.get("plain") or {}
    return str(plain.get("value") or "").strip()
