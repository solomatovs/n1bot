"""Словарь поверхностей {schema}.surface: перечисление для потребителя.

Поверхность объявляет её владелец — значением типа surface_e и строкой словаря с
описанием. Потребителю (поиск, api, чат) нужен список: показать выбор в интерфейсе,
проверить фильтр запроса. Полным списком считаются значения типа: строка словаря
может отстать от значения, а node с этим значением уже есть, и молча пропасть из
списка она не должна.

Значениями surface_e названы и поверхности рёбер (cfl_page_link), у которых нет ни
узлов, ни индекса. Их отделяет число объявленных аспектов: аспектов нет — в индекс
поверхность не попадает, и предлагать её для выбора в поиске незачем.

Ошибки: своих нет — реестр только читается, отказ базы уходит наверх psycopg.Error.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any, ClassVar, LiteralString

import psycopg
from pydantic import BaseModel, ConfigDict

from boba.ix_core.schema_name import SchemaName

__all__ = ["Surface", "SurfaceCatalog"]


class Surface(BaseModel):
    """Одна поверхность: значение surface_e, описание из словаря, число аспектов,
    объявленных её владельцем, и число node в базе."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    aspects: int
    nodes: int


class SurfaceCatalog:
    """Чтение словаря поверхностей."""

    ALL: ClassVar[LiteralString] = """
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
            left join {schema}.surface s on s.name::varchar = e.enumlabel::varchar
            left join {schema}.surface_aspect sa
                on sa.surface::varchar = e.enumlabel::varchar
        where 1=1
            and ns.nspname = %(schema)s
            and t.typname = 'surface_e'
        group by
            e.enumlabel
        order by
            e.enumlabel
    """

    def __init__(self, surfaces: Sequence[Surface]) -> None:
        self._surfaces = tuple(surfaces)

    @classmethod
    async def load(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> SurfaceCatalog:
        query = SchemaName.render(cls.ALL, db_schema)
        cur = await conn.execute(query, {"schema": db_schema})
        rows = await cur.fetchall()

        return cls(list(cls._rows(rows)))

    def all(self) -> tuple[Surface, ...]:
        return self._surfaces

    def indexed(self) -> tuple[Surface, ...]:
        """Поверхности с объявленными аспектами: только они попадают в индексы, и
        только их имеет смысл предлагать для выбора."""
        found: list[Surface] = []
        for surface in self._surfaces:
            if surface.aspects > 0:
                found.append(surface)

        return tuple(found)

    def names(self) -> tuple[str, ...]:
        found: list[str] = []
        for surface in self._surfaces:
            found.append(surface.name)

        return tuple(found)

    def unknown(self, names: Iterable[str]) -> tuple[str, ...]:
        """Имена, которых в словаре нет: фильтр по ним ничего не значит."""
        known = set(self.names())
        strange: list[str] = []
        for name in names:
            if name not in known:
                strange.append(name)

        return tuple(strange)

    @staticmethod
    def _rows(rows: Iterable[Sequence[Any]]) -> Iterator[Surface]:
        for name, description, aspects, nodes in rows:
            yield Surface(
                name=str(name),
                description=str(description),
                aspects=int(aspects),
                nodes=int(nodes),
            )
