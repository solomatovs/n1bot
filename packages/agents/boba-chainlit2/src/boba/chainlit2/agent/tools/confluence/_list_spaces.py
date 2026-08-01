"""Tool confluence_list_spaces + ConfluenceListSpacesConfig.

LLM-callable read-only tool: возвращает markdown-таблицу спейсов,
доступных текущей роли. Используется перед confluence_ingest_spaces/
confluence_download, чтобы LLM мог увидеть существующие space-ключи
и выбрать релевантные.

Endpoint: GET /rest/api/space?limit=N&start=0[&type=global|personal].
БЕЗ expand=description.plain: с ним cwiki клампит limit до 100 и 500-тит
на глубоких offset'ах; без него limit=500 отдаёт все спейсы одним запросом
(эмпирически проверено на cwiki.apache.org: 394 спейса, next=no). Фильтр
по key/name — клиентский (REST не умеет name/text-фильтр).
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase
from typing import Annotated, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.agent.tools.confluence.connection import ConfluenceConnection
from boba.chainlit2.agent.tools.confluence.models import ConfluenceSpaceItem
from boba.chainlit2.agent.tools.confluence.request_sources import (
    ConfluencePaginator,
    ConfluenceRest,
)
from boba.chainlit2.rendering.tool_result import TableResult
from boba.transport.http import HttpProfile

__all__ = ["ConfluenceListSpacesConfig", "confluence_list_spaces"]

logger = logging.getLogger("boba.chainlit2.agent.tools.confluence.list_spaces")


class ConfluenceListSpacesConfig(BaseModel):
    """Self-contained конфиг tool'а confluence_list_spaces.

    Config-секция: [tool.kb.confluence.list.spaces].
    """

    PAGE_SIZE: ClassVar[int] = 500
    """Размер страницы REST-пагинации /rest/api/space. Сильно крупнее дефолтных
    50 — в идеале все спейсы приходят одним запросом (start=0), и мы вообще не
    доходим до глубоких offset'ов, на которых сервер падает. Если сервер
    зажимает limit своим максимумом — всё равно сильно меньше прыжков."""

    model_config = ConfigDict(extra="ignore")

    confluence: HttpProfile

    @staticmethod
    def _matches(item: ConfluenceSpaceItem, pattern: str) -> bool:
        """True, если glob-pattern (*/?/[]) матчит key или name спейса.

        Пустой pattern -> True (фильтр выключен). Матч регистронезависимый и
        per-field — fnmatch требует совпадения шаблона со ВСЕМ полем, поэтому
        для поиска по подстроке нужны звёздочки: airflow* (начинается с
        airflow), *data* (содержит data), ?-prod и т.п. REST
        /rest/api/space не умеет name/text-фильтр — фильтруем клиентски.
        Описание не используется: expand=description.plain ломает выдачу
        больших Confluence (клампит limit и 500-тит на глубоких offset'ах).
        """
        if not pattern:
            return True
        p = pattern.lower()
        return any(
            fnmatchcase(field.lower(), p)
            for field in (item.key, item.name)
        )


def confluence_list_spaces(
    cfg: ConfluenceListSpacesConfig,
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
    glob = pattern.strip() if pattern else ""
    rows: list[dict[str, str]] = []
    truncated = False
    errored = False

    path = ConfluenceRest.space_list_path(
        space_type,
        limit=ConfluenceListSpacesConfig.PAGE_SIZE,
    )
    with ConfluencePaginator(ConfluenceConnection(profile=cfg.confluence)) as x:
        try:
            for item in x(path, item=ConfluenceSpaceItem):
                if not ConfluenceListSpacesConfig._matches(item, glob):
                    continue
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append({
                    "key": item.key.strip(),
                    "name": item.name.strip(),
                    "type": item.type.strip(),
                })
        except httpx.HTTPError as e:
            errored = True
            logger.warning(
                "confluence_list_spaces: пагинация прервана (%s): %s; "
                "возвращаю %d найденных спейсов",
                type(e).__name__,
                e,
                len(rows),
            )

    filter_note = f" по шаблону {glob!r}" if glob else ""
    if errored:
        note = (
            f"СПИСОК НЕПОЛНЫЙ{filter_note}: Confluence вернул ошибку при "
            f"глубокой пагинации /rest/api/space — просмотрены не все спейсы "
            f"(собрано {len(rows)}). Сузь поиск шаблоном или повтори позже."
        )
    elif truncated:
        note = (
            f"показаны первые {limit}{filter_note}; увеличьте `limit` для остальных"
        )
    elif not rows:
        note = f"ничего не найдено{filter_note}"
    else:
        note = None
    return TableResult(rows=rows, note=note)
