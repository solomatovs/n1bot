"""Tool confluence_list_spaces: список пространств Confluence.

Запрос к REST делает payload в песочнице; glob-фильтр по key/name остаётся
здесь — он дешёвый и не требует сети.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.tool.kb.confluence.caller import ConfluenceCaller
from boba.tool.kb.confluence.protocol import (
    ConfluenceSpace,
    ConfluenceSpacesRequest,
)
from boba.toolkit.result import TableResult
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceListSpacesConfig", "confluence_list_spaces"]


class ConfluenceListSpacesConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_list_spaces."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile
    page_size: int = Field(
        default=500,
        ge=1,
        le=1000,
        description="Сколько спейсов запрашивать у REST за раз.",
    )


class SpaceFilter:
    """Клиентский glob по key/name: REST не умеет фильтровать по имени."""

    @staticmethod
    def matches(space: ConfluenceSpace, pattern: str | None) -> bool:
        if not pattern:
            return True
        needle = pattern.lower()
        for field in (space.key, space.name):
            if fnmatchcase(field.lower(), needle):
                return True
        return False


def confluence_list_spaces(
    cfg: ConfluenceListSpacesConfig,
    caller: ConfluenceCaller,
    pattern: Annotated[
        str | None,
        Field(
            description=(
                "Glob-шаблон (регистронезависимо) для key/name спейса. "
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
) -> TableResult:
    """Список spaces confluence (с опциональным glob-фильтром).

    Возвращает TableResult со строками {key, name, type}. pattern
    (glob */?) сужает выдачу клиентски по key/name. Без expand весь
    список приходит одним запросом (limit=PAGE_SIZE), без глубокой пагинации.
    """
    request = ConfluenceSpacesRequest(
        op=ConfluenceSpacesRequest.OP,
        base_url=cfg.confluence.base_url or "",
        profile=ConfluenceCaller.transport_of(cfg.confluence),
        space_type=space_type,
        limit=cfg.page_size,
    )
    rows: list[dict[str, Any]] = []
    for space in caller.spaces(request).spaces:
        if not SpaceFilter.matches(space, pattern):
            continue
        rows.append(space.model_dump())
        if len(rows) >= limit:
            break
    filter_note = ""
    if pattern:
        filter_note = f" по шаблону {pattern!r}"
    note = f"спейсов: {len(rows)}{filter_note}"
    if not rows:
        note = f"ничего не найдено{filter_note}"
    return TableResult(rows=rows, note=note)
