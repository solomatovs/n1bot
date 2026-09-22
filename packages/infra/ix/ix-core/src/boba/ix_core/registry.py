"""Реестры схемы ix одним объектом: словарь поверхностей, словарь аспектов с
парами объявлений, таблицы индексов, формулы ссылок и промпты описания.

Оркестратор (поиск, инструмент kb, накат схемы, воркеры индексов и описателя)
создаёт IxRegistry со схемой и заполняет его с соединения: целиком через read или
по частям через read_*, когда таблица части ещё может не существовать. Дальше
объект отвечает на вопросы без базы: какие поверхности индексируются, каких имён
словарь не знает, какие таблицы у вида, какая ссылка у объекта, какой промпт у пары.
Объявления аспектов по классам и покрытие таблицы индекса читаются на месте, они
нужны воркерам по-разному и в реестре не живут.

Ошибки:
SurfaceUrlError — формула ссылки из реестра не разбирается.
SurfacePromptError — запрошена пара, которой в реестре промптов нет.
psycopg.Error — база отклонила запрос; уходит наверх как есть.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Self

import psycopg
from psycopg import sql

from boba.db.postgres.query import PgQueryBuilder
from boba.ix_core.aspects import AspectClass, AspectEntry, SurfaceAspect
from boba.ix_core.indexes import IndexKind, IndexTable
from boba.ix_core.prompts import SurfacePrompt, SurfacePromptError
from boba.ix_core.surfaces import Surface
from boba.ix_core.urls import SurfaceUrlError, UrlTemplate

__all__ = ["IxRegistry"]


class IxRegistry:
    """Реестры схемы ix, прочитанные один раз и живущие весь цикл потребителя."""

    def __init__(self, db_schema: str) -> None:
        self._schema = db_schema
        self._surfaces: tuple[Surface, ...] = ()
        self._aspects: tuple[AspectEntry, ...] = ()
        self._pairs: tuple[tuple[str, str], ...] = ()
        self._tables: tuple[IndexTable, ...] = ()
        self._urls: dict[str, UrlTemplate] = {}
        self._prompts: dict[tuple[str, str], SurfacePrompt] = {}

    @property
    def db_schema(self) -> str:
        return self._schema

    async def read(self, conn: psycopg.AsyncConnection[Any]) -> Self:
        """Все реестры разом: для потребителя, у которого схема уже накачена."""
        await self.read_surfaces(conn)
        await self.read_aspects(conn)
        await self.read_tables(conn)
        await self.read_urls(conn)
        await self.read_prompts(conn)

        return self

    async def read_surfaces(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Поверхности это значения surface_e: строка словаря может отстать от
        значения, а node с ним уже есть, и пропасть из списка она не должна."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select
                    e.enumlabel::varchar,
                    coalesce(max(s.description), ''),
                    count(distinct sa.aspect),
                    (
                        select count(*)
                        from {schema}.node n
                        where n.surface = e.enumlabel::{schema}.surface_e
                    )
                from
                    pg_type t
                    join pg_namespace ns on ns.oid = t.typnamespace
                    join pg_enum e on e.enumtypid = t.oid
                    left join {schema}.surface s
                        on s.name::varchar = e.enumlabel::varchar
                    left join {schema}.surface_aspect sa
                        on sa.surface::varchar = e.enumlabel::varchar
                where 1=1
                    and ns.nspname = %(schema_name)s
                    and t.typname = 'surface_e'
                group by
                    e.enumlabel
                order by
                    e.enumlabel
                """,
                schema_name=self._schema,
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        found: list[Surface] = []
        async for name, description, aspects, nodes in cur:
            found.append(
                Surface(
                    name=str(name),
                    description=str(description),
                    aspects=int(aspects),
                    nodes=int(nodes),
                )
            )

        self._surfaces = tuple(found)

    async def read_aspects(self, conn: psycopg.AsyncConnection[Any]) -> None:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
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
                """
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        entries: list[AspectEntry] = []
        async for aspect, aspect_class, description, owner in cur:
            entries.append(
                AspectEntry(
                    aspect=str(aspect),
                    aspect_class=AspectClass(str(aspect_class)),
                    description=str(description),
                    owner=str(owner),
                )
            )

        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
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
                """
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        pairs: list[tuple[str, str]] = []
        async for surface, aspect in cur:
            pairs.append((str(surface), str(aspect)))

        self._aspects = tuple(entries)
        self._pairs = tuple(pairs)

    async def read_tables(self, conn: psycopg.AsyncConnection[Any]) -> None:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select
                    t.kind::varchar,
                    t.name,
                    t.owner
                from
                    {schema}.index_table t
                order by
                    t.kind,
                    t.name
                """
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        found: list[IndexTable] = []
        async for kind, name, owner in cur:
            found.append(
                IndexTable(kind=IndexKind(str(kind)), name=str(name), owner=str(owner))
            )

        self._tables = tuple(found)

    async def read_urls(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Формулы ссылок; отдаёт поверхности, у которых формула есть."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select
                    u.surface::varchar,
                    u.template
                from
                    {schema}.surface_url u
                order by
                    u.surface
                """
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        templates: dict[str, UrlTemplate] = {}
        async for surface, template in cur:
            try:
                templates[str(surface)] = UrlTemplate(str(template))
            except SurfaceUrlError as exc:
                raise SurfaceUrlError(f"surface {surface}: {exc}") from exc

        self._urls = templates

    async def read_prompts(self, conn: psycopg.AsyncConnection[Any]) -> None:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
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
                """
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        found: dict[tuple[str, str], SurfacePrompt] = {}
        async for (
            surface,
            aspect,
            system_prompt,
            user_template,
            owner,
        ) in cur:
            prompt = SurfacePrompt(
                surface=str(surface),
                aspect=str(aspect),
                system_prompt=str(system_prompt),
                user_template=str(user_template),
                owner=str(owner),
            )
            found[prompt.key()] = prompt

        self._prompts = found

    async def read_declarations(
        self, conn: psycopg.AsyncConnection[Any], classes: Sequence[AspectClass]
    ) -> tuple[SurfaceAspect, ...]:
        """Объявления аспектов заданных классов с телами; пустой список классов
        значит все объявления."""
        names: list[str] = []
        for item in classes:
            names.append(str(item))

        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select
                    sa.surface::varchar,
                    sa.aspect::varchar,
                    sa.body
                from
                    {schema}.surface_aspect sa
                    join {schema}.aspect a on a.aspect = sa.aspect
                where 1=1
                """
            )
            .when(bool(names), "and a.class::varchar = any(%(classes)s)", classes=names)
            .add(
                """
                order by
                    sa.surface,
                    sa.aspect
                """
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        found: list[SurfaceAspect] = []
        async for surface, aspect, body in cur:
            found.append(
                SurfaceAspect(surface=str(surface), aspect=str(aspect), body=str(body))
            )

        return tuple(found)

    async def read_coverage(
        self, conn: psycopg.AsyncConnection[Any], table: IndexTable
    ) -> frozenset[tuple[str, str]]:
        """Пары «поверхность, аспект», которые таблица индекса несёт на самом деле:
        какие аспекты в неё попали, решает подписка её владельца на классы."""
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(
                """
                select distinct
                    t.surface::varchar,
                    t.aspect::varchar
                from
                    {schema}.{index} t
                """,
                index=table.ident(),
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)

        pairs: set[tuple[str, str]] = set()
        async for surface, aspect in cur:
            pairs.add((str(surface), str(aspect)))

        return frozenset(pairs)

    def union_sources(self, declarations: Sequence[SurfaceAspect]) -> sql.Composed:
        """Тела объявлений одним `union all` с колонками surface, aspect, node_id,
        content — источник для файлов run/ потребителя вместо `{sources}`."""
        schema = sql.Identifier(self._schema)
        if not declarations:
            return (
                PgQueryBuilder(schema=schema)
                .add(
                    """
                    select
                        null::{schema}.surface_e as surface,
                        null::{schema}.aspect_e as aspect,
                        null::bigint as node_id,
                        null::varchar as content
                    where
                        false
                    """
                )
                .build()
                .text
            )

        parts = PgQueryBuilder(schema=schema)
        for position, declaration in enumerate(declarations):
            body = PgQueryBuilder(schema=schema).add(declaration.body).build()
            parts.when(position > 0, "union all")
            parts.add(
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
                surface=sql.Literal(declaration.surface),
                aspect=sql.Literal(declaration.aspect),
                body=body.text,
            )

        return parts.build().text

    def get_surfaces(self) -> tuple[Surface, ...]:
        return self._surfaces

    def indexed_surfaces(self) -> tuple[Surface, ...]:
        """Поверхности с объявленными аспектами: только они попадают в индексы, и
        только их имеет смысл предлагать для выбора."""
        found: list[Surface] = []
        for surface in self._surfaces:
            if surface.aspects > 0:
                found.append(surface)

        return tuple(found)

    def surface_names(self) -> tuple[str, ...]:
        found: list[str] = []
        for surface in self._surfaces:
            found.append(surface.name)

        return tuple(found)

    def unknown_surfaces(self, names: Iterable[str]) -> tuple[str, ...]:
        known = set(self.surface_names())
        strange: list[str] = []
        for name in names:
            if name not in known:
                strange.append(name)

        return tuple(strange)

    def get_aspects(self) -> tuple[AspectEntry, ...]:
        return self._aspects

    def aspect_names(self) -> tuple[str, ...]:
        found: list[str] = []
        for entry in self._aspects:
            found.append(entry.aspect)

        return tuple(found)

    def aspects_of(self, surface: str) -> tuple[AspectEntry, ...]:
        """Аспекты, объявленные для поверхности, в порядке словаря."""
        by_name: dict[str, AspectEntry] = {}
        for entry in self._aspects:
            by_name[entry.aspect] = entry

        found: list[AspectEntry] = []
        for pair_surface, aspect in self._pairs:
            if pair_surface != surface:
                continue

            entry = by_name.get(aspect)
            if entry is not None:
                found.append(entry)

        return tuple(found)

    def unknown_aspects(self, names: Iterable[str]) -> tuple[str, ...]:
        known = set(self.aspect_names())
        strange: list[str] = []
        for name in names:
            if name not in known:
                strange.append(name)

        return tuple(strange)

    def tables_of(self, kind: IndexKind) -> tuple[IndexTable, ...]:
        found: list[IndexTable] = []
        for table in self._tables:
            if table.kind is kind:
                found.append(table)

        return tuple(found)

    def get_tables(self) -> tuple[IndexTable, ...]:
        return self._tables

    def get_urls(self) -> tuple[str, ...]:
        return tuple(sorted(self._urls))

    def url_of(self, surface: str, address: Mapping[str, Any]) -> str:
        """Ссылка на объект; у поверхности без формулы её нет, и это пустая строка."""
        template = self._urls.get(surface)
        if template is None:
            return ""

        return template.render(address)

    def get_prompts(self) -> tuple[SurfacePrompt, ...]:
        return tuple(self._prompts.values())

    def prompt_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._prompts)

    def prompt_of(self, surface: str, aspect: str) -> SurfacePrompt:
        prompt = self._prompts.get((surface, aspect))
        if prompt is None:
            raise SurfacePromptError(
                f"prompt for {surface}/{aspect}: no row in surface_prompt; "
                f"registered pairs are {list(self._prompts)}"
            )

        return prompt
