"""Объявления аспектов: классы, словарь, чтение объявлений из базы, проверка
контракта тела и сборка одного источника для потребителя.

Потребитель (индексатор, описатель) подписан на классы аспектов. Он читает
объявления своих классов из {schema}.surface_aspect, а AspectSources склеивает
их тела в один `union all` с колонками surface, aspect, node_id, content, который
потребитель подставляет в свои файлы run/ вместо `{sources}`. Поиск читает словарь
{schema}.aspect целиком (AspectCatalog): показать выбор, проверить фильтр запроса,
узнать класс аспекта строки выдачи.

Ошибки:
AspectDeclarationError — тело объявления не выполняется на этой базе или
    отдаёт не те колонки, что требует контракт (node_id bigint, content varchar).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg.postgres import types as pg_types
from pydantic import BaseModel, ConfigDict

from boba.db.postgres.query import PgQueryBuilder
from boba.toolkit.sql import QueryBuildError

__all__ = [
    "AspectCatalog",
    "AspectClass",
    "AspectContract",
    "AspectDeclarationError",
    "AspectDeclarations",
    "AspectEntry",
    "AspectSources",
    "SurfaceAspect",
]


class AspectDeclarationError(Exception):
    """Объявление аспекта не отвечает контракту."""


class AspectClass(StrEnum):
    """Класс аспекта: что это за текст; значения enum aspect_class_e."""

    IDENT = "ident"
    WORDS = "words"
    DESCRIPTION = "description"
    DESCRIBER_INPUT = "describer_input"


class ContractColumn(StrEnum):
    """Колонки, которые обязано вернуть тело объявления, в этом порядке."""

    NODE_ID = "node_id"
    CONTENT = "content"


class ContractType(StrEnum):
    """Имена типов postgres, допустимых для колонок контракта."""

    INT8 = "int8"
    TEXT = "text"
    VARCHAR = "varchar"


class AspectEntry(BaseModel):
    """Одна строка словаря {schema}.aspect: имя, класс, описание и владелец."""

    model_config = ConfigDict(frozen=True)

    aspect: str
    aspect_class: AspectClass
    description: str
    owner: str


class AspectCatalog:
    """Словарь аспектов и пары «поверхность, аспект» из объявлений: что вообще есть,
    какие аспекты у поверхности и какие имена запроса словарю неизвестны."""

    def __init__(
        self, entries: Sequence[AspectEntry], pairs: Sequence[tuple[str, str]]
    ) -> None:
        self._entries = tuple(entries)
        self._by_name: dict[str, AspectEntry] = {}
        for entry in entries:
            self._by_name[entry.aspect] = entry

        self._of_surface: dict[str, list[AspectEntry]] = {}
        for surface, aspect in pairs:
            entry = self._by_name.get(aspect)
            if entry is None:
                continue

            self._of_surface.setdefault(surface, []).append(entry)

    @classmethod
    async def load(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> AspectCatalog:
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    a.aspect::varchar,
                    a.class::varchar,
                    a.description,
                    a.owner
                from
                    {schema}.aspect a
                order by
                    a.class,
                    a.aspect
                """,
                schema=sql.Identifier(db_schema),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        entries = list(cls._parse(await cur.fetchall()))

        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    sa.surface::varchar,
                    sa.aspect::varchar
                from
                    {schema}.surface_aspect sa
                order by
                    sa.surface,
                    sa.aspect
                """,
                schema=sql.Identifier(db_schema),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        pairs: list[tuple[str, str]] = []
        for surface, aspect in await cur.fetchall():
            pairs.append((str(surface), str(aspect)))

        return cls(entries, pairs)

    def all(self) -> tuple[AspectEntry, ...]:
        return self._entries

    def names(self) -> tuple[str, ...]:
        found: list[str] = []
        for entry in self._entries:
            found.append(entry.aspect)

        return tuple(found)

    def of_surface(self, surface: str) -> tuple[AspectEntry, ...]:
        """Аспекты, объявленные для поверхности, в порядке словаря."""
        return tuple(self._of_surface.get(surface, ()))

    def unknown(self, names: Iterable[str]) -> tuple[str, ...]:
        """Имена, которых в словаре нет: фильтр по ним ничего не значит."""
        strange: list[str] = []
        for name in names:
            if name not in self._by_name:
                strange.append(name)

        return tuple(strange)

    @staticmethod
    def _parse(rows: Iterable[Sequence[Any]]) -> Iterator[AspectEntry]:
        for aspect, aspect_class, description, owner in rows:
            yield AspectEntry(
                aspect=str(aspect),
                aspect_class=AspectClass(str(aspect_class)),
                description=str(description),
                owner=str(owner),
            )


class SurfaceAspect(BaseModel):
    """Одна строка {schema}.surface_aspect."""

    model_config = ConfigDict(frozen=True)

    surface: str
    aspect: str
    body: str


class AspectDeclarations:
    """Чтение объявлений из {schema}.surface_aspect: все или только заданных классов."""

    @classmethod
    async def all(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> list[SurfaceAspect]:
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    sa.surface::varchar,
                    sa.aspect::varchar,
                    sa.body
                from
                    {schema}.surface_aspect sa
                order by
                    sa.surface,
                    sa.aspect
                """,
                schema=sql.Identifier(db_schema),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows = await cur.fetchall()

        return list(cls._rows(rows))

    @classmethod
    async def of_classes(
        cls,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        classes: Sequence[AspectClass],
    ) -> list[SurfaceAspect]:
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    sa.surface::varchar,
                    sa.aspect::varchar,
                    sa.body
                from
                    {schema}.surface_aspect sa
                    join {schema}.aspect a on a.aspect = sa.aspect
                where
                    a.class::varchar = any(%(classes)s)
                order by
                    sa.surface,
                    sa.aspect
                """,
                schema=sql.Identifier(db_schema),
                classes=cls._names(classes),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows = await cur.fetchall()

        return list(cls._rows(rows))

    @classmethod
    async def of_surfaces(
        cls,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        surfaces: Sequence[str],
        classes: Sequence[AspectClass],
    ) -> list[SurfaceAspect]:
        """Объявления своих поверхностей: индексатор-владелец читает только их,
        чтобы не пробовать чужие тела на каждом node."""
        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    sa.surface::varchar,
                    sa.aspect::varchar,
                    sa.body
                from
                    {schema}.surface_aspect sa
                    join {schema}.aspect a on a.aspect = sa.aspect
                where
                    a.class::varchar = any(%(classes)s)
                    and sa.surface::varchar = any(%(surfaces)s)
                order by
                    sa.surface,
                    sa.aspect
                """,
                schema=sql.Identifier(db_schema),
                classes=cls._names(classes),
                surfaces=list(surfaces),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        rows = await cur.fetchall()

        return list(cls._rows(rows))

    @staticmethod
    def _names(classes: Sequence[AspectClass]) -> list[str]:
        names: list[str] = []
        for item in classes:
            names.append(str(item))

        return names

    @staticmethod
    def _rows(rows: Iterable[Sequence[Any]]) -> Iterator[SurfaceAspect]:
        for surface, aspect, body in rows:
            yield SurfaceAspect(
                surface=str(surface),
                aspect=str(aspect),
                body=str(body),
            )


class AspectContract:
    """Проверка тел объявлений: выполняется ли и те ли колонки отдаёт."""

    @classmethod
    async def check(
        cls,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        declarations: Sequence[SurfaceAspect],
    ) -> None:
        for declaration in declarations:
            await cls._check_one(conn, db_schema, declaration)

    @classmethod
    async def _check_one(
        cls,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        declaration: SurfaceAspect,
    ) -> None:
        where = f"aspect {declaration.aspect} of surface {declaration.surface}"

        try:
            body = (
                PgQueryBuilder()
                .add(declaration.body, schema=sql.Identifier(db_schema))
                .build()
            )
        except QueryBuildError as exc:
            raise AspectDeclarationError(f"{where}: body: {exc}") from exc

        probe = (
            PgQueryBuilder()
            .add("select * from ({body}) s limit 0", body=body.text)
            .build()
        )
        try:
            cur = await conn.execute(probe.text, probe.params)
        except psycopg.Error as exc:
            raise AspectDeclarationError(f"{where}: body does not run: {exc}") from exc

        if cur.description is None:
            raise AspectDeclarationError(f"{where}: body returns no result set")

        names: list[str] = []
        type_names: list[str] = []
        for column in cur.description:
            names.append(column.name)
            type_names.append(cls._type_name(column.type_code))

        expected = [ContractColumn.NODE_ID, ContractColumn.CONTENT]
        if names != expected:
            raise AspectDeclarationError(
                f"{where}: body must return columns {expected}, got {names}"
            )

        if type_names[0] != ContractType.INT8:
            raise AspectDeclarationError(
                f"{where}: {ContractColumn.NODE_ID} must be {ContractType.INT8}, "
                f"got {type_names[0]}"
            )

        content_types = frozenset({ContractType.TEXT, ContractType.VARCHAR})
        if type_names[1] not in content_types:
            raise AspectDeclarationError(
                f"{where}: {ContractColumn.CONTENT} must be one of "
                f"{sorted(content_types)}, got {type_names[1]}"
            )

    @staticmethod
    def _type_name(oid: int) -> str:
        info = pg_types.get(oid)
        if info is None:
            return f"oid {oid}"

        return info.name


class AspectSources:
    """Сборка одного источника из объявлений для подстановки вместо `{sources}`."""

    @classmethod
    def union(
        cls, declarations: Sequence[SurfaceAspect], db_schema: str
    ) -> sql.Composed:
        schema = sql.Identifier(db_schema)

        if not declarations:
            return (
                PgQueryBuilder()
                .add(
                    """
                select
                    null::{schema}.surface_e as surface,
                    null::{schema}.aspect_e as aspect,
                    null::bigint as node_id,
                    null::varchar as content
                where
                    false
                    """,
                    schema=schema,
                )
                .build()
                .text
            )

        parts: list[sql.Composed] = []
        for declaration in declarations:
            parts.append(
                PgQueryBuilder()
                .add(
                    """
                    select
                        {surface}::{schema}.surface_e as surface,
                        {aspect}::{schema}.aspect_e as aspect,
                        s.node_id::bigint as node_id,
                        s.content::varchar as content
                    from
                        ({body}) s
                    where
                        s.content is not null
                        and s.content <> ''
                    """,
                    schema=schema,
                    surface=sql.Literal(declaration.surface),
                    aspect=sql.Literal(declaration.aspect),
                    body=PgQueryBuilder()
                    .add(declaration.body, schema=sql.Identifier(db_schema))
                    .build()
                    .text,
                )
                .build()
                .text
            )

        return sql.SQL("\n    union all").join(parts)
