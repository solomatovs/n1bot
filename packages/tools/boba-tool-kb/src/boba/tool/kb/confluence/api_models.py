"""Pydantic-модели для Confluence REST-API discovery-эндпоинтов.

Используются `iter_paginated[T]` для типизированного парсинга элементов
страничного ответа:

- `ConfluencePageItem`  — page-объект из `/rest/api/content/{id}`,
                          `/rest/api/content/search?cql=…`,
                          `/rest/api/space/{key}/content?type=page`.
- `ConfluenceSpaceItem` — space-объект из `/rest/api/space`.

Все модели — `extra="ignore"`: Confluence REST в разных версиях возвращает
кучу полей, которые нам не нужны. Padding'ом не страдаем.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ConfluencePageItem(BaseModel):
    """Один page-result из Confluence discovery-эндпоинтов.

    Из всех полей discovery нам нужен только `id` — он передаётся в
    `/rest/api/content/{id}?expand=…` дальше по pipeline'у. `title` оставлен
    для логов/диагностики (на cwiki/Atlassian всегда присутствует).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str = ""


class ConfluencePlainText(BaseModel):
    """Inner `description.plain` из `/rest/api/space?expand=description.plain`."""

    model_config = ConfigDict(extra="ignore")

    value: str = ""


class ConfluenceDescription(BaseModel):
    """`description` вложенный объект space'а с опциональным plain-текстом."""

    model_config = ConfigDict(extra="ignore")

    plain: ConfluencePlainText | None = None


class ConfluenceSpaceItem(BaseModel):
    """Один space-result из `/rest/api/space?[type=…][&expand=description.plain]`.

    `description` — заполняется только при `expand=description.plain`. В
    остальных случаях None. Используем property `description_plain` для
    удобного доступа без `.description.plain.value` цепочки.
    """

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str = ""
    type: str = ""
    description: ConfluenceDescription | None = None

    @property
    def description_plain(self) -> str:
        """Plain-text описание space'а или пустая строка."""
        if self.description and self.description.plain:
            return self.description.plain.value
        return ""
