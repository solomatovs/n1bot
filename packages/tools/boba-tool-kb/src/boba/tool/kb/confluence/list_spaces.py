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

from fnmatch import fnmatchcase
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from boba.markdown import format_markdown_table
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import ConfluenceSpaceItem
from boba.tool.kb.confluence.request_sources import ConfluencePaginator, ConfluenceRest
from boba.tools import FromConfig, tool

__all__ = ["ConfluenceListSpacesConfig", "confluence_list_spaces"]


class ConfluenceListSpacesConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `confluence_list_spaces`.

    Config-секция: `[tool.kb.confluence.list.spaces]`.
    """

    MAX_CELL_CHARS: ClassVar[int] = 200

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence.list.spaces",
        defaults_from=("confluence",),
    )

    confluence: ConfluenceConnection

    @staticmethod
    def _matches(item: ConfluenceSpaceItem, pattern: str) -> bool:
        """True, если glob-`pattern` (`*`/`?`/`[]`) матчит key, name или description.

        Пустой `pattern` → True (фильтр выключен). Матч регистронезависимый и
        per-field — fnmatch требует совпадения шаблона со ВСЕМ полем, поэтому
        для поиска по подстроке нужны звёздочки: `airflow*` (начинается с
        `airflow`), `*data*` (содержит `data`), `?-prod` и т.п. REST
        `/rest/api/space` не умеет name/text-фильтр — фильтруем клиентски в
        потоке пагинации.
        """
        if not pattern:
            return True
        p = pattern.lower()
        return any(
            fnmatchcase(field.lower(), p)
            for field in (item.key, item.name, item.description_plain)
        )


@tool
def confluence_list_spaces(
    cfg: Annotated[ConfluenceListSpacesConfig, FromConfig()],
    pattern: Annotated[
        str | None,
        Field(
            description=(
                "Glob-шаблон (регистронезависимо) для key/name/description спейса. "
                "`*` — любая последовательность символов, `?` — один символ. "
                "Шаблон должен совпасть с полем ЦЕЛИКОМ, поэтому для поиска по "
                "подстроке оборачивай в звёздочки. Примеры: `airflow*` "
                "(начинается с `airflow`), `*data*` (содержит `data`), "
                "`*-prod` (заканчивается на `-prod`). Не передавай (или `null`) "
                "— вернуть все спейсы выбранного типа. Полезно на больших "
                "Confluence с сотнями спейсов."
            ),
        ),
    ] = None,
    space_type: Annotated[
        Literal["global", "personal", "any"],
        Field(
            description=(
                "Фильтр по типу space'а: `global` (командные), `personal` "
                "(личные user'ов), `any` (без фильтра, оба типа). "
                "По умолчанию `global`"
            ),
        ),
    ] = "global",
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=1000,
            description=(
                "Hard cap на количество возвращаемых (после фильтра) spaces — "
                "защита от огромного payload'а на больших Confluence-серверах. "
                "Если совпадений больше — таблица помечается как truncated."
            ),
        ),
    ] = 200,
) -> str:
    """Список spaces confluence (с опциональным glob-фильтром).

    Возвращает markdown с колонками `key, name, type, description`. `pattern`
    (glob `*`/`?`) сужает выдачу клиентски по key/name/description.
    """
    glob = pattern.strip() if pattern else ""
    rows: list[tuple[Any, ...]] = []
    truncated = False
    with ConfluencePaginator(cfg.confluence) as x:
        for item in x(
            ConfluenceRest.space_list_path(space_type, expand="description.plain"),
            item=ConfluenceSpaceItem,
        ):
            if not ConfluenceListSpacesConfig._matches(item, glob):
                continue
            if len(rows) >= limit:
                truncated = True
                break
            rows.append((
                item.key.strip(),
                item.name.strip(),
                item.type.strip(),
                item.description_plain,
            ))

    filter_note = f" по шаблону {glob!r}" if glob else ""
    table_md = format_markdown_table(
        columns=["key", "name", "type", "description"],
        rows=rows,
        max_cell_chars=ConfluenceListSpacesConfig.MAX_CELL_CHARS,
        truncated=truncated,
        truncated_msg=(
            f"more spaces omitted{filter_note} (увеличьте limit, текущий {limit})"
        ),
    )
    return table_md
