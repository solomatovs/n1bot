"""Объявления аспектов: классы, чтение объявлений из базы, проверка контракта тела
и сборка одного источника для потребителя.

Потребитель (индексатор, описатель) подписан на классы аспектов. Он читает
объявления своих классов из {schema}.surface_aspect, а AspectSources склеивает
их тела в один `union all` с колонками surface, aspect, node_id, content, который
потребитель подставляет в свои файлы run/ вместо `{sources}`.

Ошибки:
AspectDeclarationError — тело объявления не выполняется на этой базе или
    отдаёт не те колонки, что требует контракт (node_id bigint, content varchar).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import ClassVar, LiteralString

import psycopg
from psycopg import sql
from psycopg.postgres import types as pg_types
from pydantic import BaseModel, ConfigDict

from boba.pg_ix_core.schema_name import SchemaName, SchemaNameError

__all__ = [
    "AspectClass",
    "AspectContract",
    "AspectDeclarationError",
    "AspectDeclarations",
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


class SurfaceAspect(BaseModel):
    """Одна строка {schema}.surface_aspect."""

    model_config = ConfigDict(frozen=True)

    surface: str
    aspect: str
    body: str


class AspectDeclarations:
    """Чтение объявлений из {schema}.surface_aspect: все или только заданных классов."""

    ALL: ClassVar[str] = """
        select sa.surface::varchar, sa.aspect::varchar, sa.body
        from {schema}.surface_aspect sa
        order by sa.surface, sa.aspect
    """
    OF_CLASSES: ClassVar[str] = """
        select sa.surface::varchar, sa.aspect::varchar, sa.body
        from {schema}.surface_aspect sa
        join {schema}.aspect a on a.aspect = sa.aspect
        where a.class::varchar = any(%(classes)s)
        order by sa.surface, sa.aspect
    """

    @classmethod
    def all(cls, conn: psycopg.Connection, db_schema: str) -> list[SurfaceAspect]:
        cur = conn.execute(SchemaName.render(cls.ALL, db_schema))

        return list(cls._rows(cur))

    @classmethod
    def of_classes(
        cls,
        conn: psycopg.Connection,
        db_schema: str,
        classes: Sequence[AspectClass],
    ) -> list[SurfaceAspect]:
        names: list[str] = []
        for item in classes:
            names.append(str(item))

        cur = conn.execute(
            SchemaName.render(cls.OF_CLASSES, db_schema), {"classes": names}
        )

        return list(cls._rows(cur))

    @staticmethod
    def _rows(cur: psycopg.Cursor) -> Iterator[SurfaceAspect]:
        for surface, aspect, body in cur.fetchall():
            yield SurfaceAspect(
                surface=str(surface), aspect=str(aspect), body=str(body)
            )


class AspectContract:
    """Проверка тел объявлений: выполняется ли и те ли колонки отдаёт."""

    PROBE: ClassVar[LiteralString] = "select * from ({body}) s limit 0"
    CONTENT_TYPES: ClassVar[frozenset[str]] = frozenset(
        {ContractType.TEXT, ContractType.VARCHAR}
    )

    @classmethod
    def check(
        cls,
        conn: psycopg.Connection,
        db_schema: str,
        declarations: Sequence[SurfaceAspect],
    ) -> None:
        for declaration in declarations:
            cls._check_one(conn, db_schema, declaration)

    @classmethod
    def _check_one(
        cls, conn: psycopg.Connection, db_schema: str, declaration: SurfaceAspect
    ) -> None:
        where = f"aspect {declaration.aspect} of surface {declaration.surface}"

        try:
            body = SchemaName.render(declaration.body, db_schema)
        except SchemaNameError as exc:
            raise AspectDeclarationError(f"{where}: body: {exc}") from exc

        probe = sql.SQL(cls.PROBE).format(body=body)
        try:
            cur = conn.execute(probe)
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

        if type_names[1] not in cls.CONTENT_TYPES:
            raise AspectDeclarationError(
                f"{where}: {ContractColumn.CONTENT} must be one of "
                f"{sorted(cls.CONTENT_TYPES)}, got {type_names[1]}"
            )

    @staticmethod
    def _type_name(oid: int) -> str:
        info = pg_types.get(oid)
        if info is None:
            return f"oid {oid}"

        return info.name


class AspectSources:
    """Сборка одного источника из объявлений для подстановки вместо `{sources}`."""

    PART: ClassVar[LiteralString] = """
    select
        {surface}::{schema}.surface_e as surface,
        {aspect}::{schema}.aspect_e as aspect,
        s.node_id::bigint as node_id,
        s.content::varchar as content
    from
        ({body}) s
    where
        s.content is not null
        and s.content <> ''"""
    EMPTY: ClassVar[LiteralString] = """
    select
        null::{schema}.surface_e as surface,
        null::{schema}.aspect_e as aspect,
        null::bigint as node_id,
        null::varchar as content
    where
        false"""
    GLUE: ClassVar[LiteralString] = "\n    union all"

    @classmethod
    def union(
        cls, declarations: Sequence[SurfaceAspect], db_schema: str
    ) -> sql.Composed:
        schema = sql.Identifier(db_schema)

        if not declarations:
            return sql.SQL(cls.EMPTY).format(schema=schema)

        parts: list[sql.Composed] = []
        for declaration in declarations:
            parts.append(
                sql.SQL(cls.PART).format(
                    schema=schema,
                    surface=sql.Literal(declaration.surface),
                    aspect=sql.Literal(declaration.aspect),
                    body=SchemaName.render(declaration.body, db_schema),
                )
            )

        return sql.SQL(cls.GLUE).join(parts)
