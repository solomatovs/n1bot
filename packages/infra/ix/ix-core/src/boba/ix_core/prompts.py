"""Промпты описания: что владелец поверхности говорит модели о её объектах.

Материал объекта даёт объявление аспекта класса describer_input, а роль модели и
правила разбора этого материала знает владелец поверхности: структура таблицы и текст
статьи объясняются по-разному. Правило живёт строкой в {schema}.surface_prompt на пару
«поверхность, аспект»; описатель читает реестр при старте и применяет, не зная ни
происхождений, ни поверхностей.

В шаблоне запроса стоит подстановка {input}: вместо неё подставляется материал объекта.
Пара без строки не описывается — общего промпта на все поверхности нет намеренно.

Ошибки:
SurfacePromptError — строка реестра не годится: пустой системный промпт, шаблон без
    подстановки материала или запрошена пара, которой в реестре нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.db.postgres.query import PgQueryBuilder

__all__ = ["SurfacePrompt", "SurfacePromptError", "SurfacePrompts"]


class SurfacePromptError(Exception):
    """Промпт поверхности не годится для описания."""


class SurfacePrompt(BaseModel):
    """Одна строка {schema}.surface_prompt: как объяснять модели объекты этой пары."""

    model_config = ConfigDict(frozen=True)

    surface: str
    aspect: str
    system_prompt: str
    user_template: str
    owner: str

    def key(self) -> tuple[str, str]:
        return (self.surface, self.aspect)

    def user(self, material: str) -> str:
        """Запрос к модели: материал объекта вместо подстановки."""
        return self.user_template.replace("{input}", material)

    def check(self) -> None:
        """Строка годится: есть роль модели и место под материал."""
        where = f"prompt for {self.surface}/{self.aspect} (owner {self.owner})"

        if not self.system_prompt.strip():
            raise SurfacePromptError(
                f"{where}: expected a system prompt, got an empty string"
            )

        if "{input}" not in self.user_template:
            raise SurfacePromptError(
                f"{where}: expected the placeholder {'{input}'} in the user template, "
                f"got {self.user_template[:80]!r}"
            )


class SurfacePrompts:
    """Чтение промптов из реестра и выдача их описателю."""

    def __init__(self, prompts: Iterable[SurfacePrompt]) -> None:
        found: dict[tuple[str, str], SurfacePrompt] = {}
        for prompt in prompts:
            found[prompt.key()] = prompt

        self._prompts = found

    @classmethod
    async def load(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> SurfacePrompts:
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    p.surface::varchar,
                    p.aspect::varchar,
                    p.system_prompt,
                    p.user_template,
                    p.owner
                from
                    {schema}.surface_prompt p
                order by
                    p.surface,
                    p.aspect
            """,
                schema=sql.Identifier(db_schema),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows = await cur.fetchall()

        return cls(cls._rows(rows))

    @classmethod
    async def check(cls, conn: psycopg.AsyncConnection[Any], db_schema: str) -> int:
        """Проверить все строки реестра; возвращает сколько их."""
        prompts = await cls.load(conn, db_schema)
        for prompt in prompts.all():
            prompt.check()

        return len(prompts.all())

    def all(self) -> tuple[SurfacePrompt, ...]:
        return tuple(self._prompts.values())

    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._prompts)

    def of(self, surface: str, aspect: str) -> SurfacePrompt:
        prompt = self._prompts.get((surface, aspect))
        if prompt is None:
            raise SurfacePromptError(
                f"prompt for {surface}/{aspect}: no row in surface_prompt; "
                f"registered pairs are {list(self._prompts)}"
            )

        return prompt

    @staticmethod
    def _rows(rows: Iterable[Sequence[Any]]) -> Iterator[SurfacePrompt]:
        for surface, aspect, system_prompt, user_template, owner in rows:
            yield SurfacePrompt(
                surface=str(surface),
                aspect=str(aspect),
                system_prompt=str(system_prompt),
                user_template=str(user_template),
                owner=str(owner),
            )
