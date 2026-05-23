"""Tool `confluence_list_spaces` + `ConfluenceListSpacesConfig`.

LLM-callable read-only tool: возвращает markdown-таблицу спейсов,
доступных текущей роли. Используется перед `confluence_ingest_spaces`/
`confluence_download`, чтобы LLM мог увидеть существующие space-ключи
и выбрать релевантные.

Endpoint: `GET /rest/api/space` с query-параметрами `limit=N&start=0`,
опциональным `&type=global|personal` и `&expand=description.plain` —
cursor-based пагинация через `_links.next`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from boba.markdown import format_markdown_table
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.api_models import ConfluenceSpaceItem
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.request_sources._common import (
    ConfluencePaginator,
    space_list_path,
)
from boba.tools import FromConfig, tool

__all__ = ["ConfluenceListSpacesConfig", "confluence_list_spaces"]

_MAX_CELL_CHARS = 200


class ConfluenceListSpacesConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_list_spaces`.

    Config-секция: `[tool.kb.confluence.list.spaces]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.list.spaces",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection


@tool
def confluence_list_spaces(
    cfg: Annotated[ConfluenceListSpacesConfig, FromConfig()],
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
    """Список spaces confluence

    Возвращает markdown с колонками `key, name, type, description`.

    """
    auth = cfg.confluence.make_auth()

    rows: list[tuple[Any, ...]] = []
    truncated = False
    with ConfluencePaginator(
        cfg.confluence.base_url,
        auth,
        cfg.confluence.timeout_sec,
    ) as x:
        for item in x(
            space_list_path(space_type, expand="description.plain"),
            item=ConfluenceSpaceItem,
        ):
            if len(rows) >= limit:
                truncated = True
                break
            rows.append((
                item.key.strip(),
                item.name.strip(),
                item.type.strip(),
                item.description_plain,
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
