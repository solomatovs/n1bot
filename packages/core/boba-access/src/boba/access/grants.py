"""Гранты соединений субъекту: один SQL, по которому хост и инструменты
узнают, какие строки connections выданы пользователю лично или любой его роли.

Здесь же, где решается доступ к инструментам, живёт и доступ к соединениям:
текст запроса один, чтобы брокер (выбор строки под вызов) и плагин
connection_list/connection_search (каталог для модели) считали выдачу и дубли
имён одинаково — имя, выданное дважды внутри одного вида, дубль, вызов по
нему неоднозначен. Домен psycopg не знает: текст несёт плейсхолдеры
идентификаторов `{...}` и параметров `%(...)s`, идентификаторы подставляет
исполнитель по картам ConnectionNames.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any, ClassVar, LiteralString

from pydantic import BaseModel, ConfigDict

from boba.connections.profile import (
    ConnectionsColumn,
    ConnectionTable,
    GrantKind,
    GrantsColumn,
    RolesColumn,
)
from boba.identity.context import Subject

__all__ = [
    "ConnectionFilter",
    "ConnectionNames",
    "FilterParam",
    "LikePattern",
    "ProfileKey",
    "SubjectGrantsQuery",
    "SubjectRowColumn",
]


class ProfileKey(StrEnum):
    """Открытые ключи jsonb профиля, по которым фильтруют и показывают строки."""

    KIND = "kind"
    HOST = "host"
    DESCRIPTION = "description"


class SubjectRowColumn(StrEnum):
    """Колонки выдачи SubjectGrantsQuery."""

    ID = "id"
    NAME = "name"
    DATA = "data"
    KIND = "kind"
    COPIES = "copies"


class ColumnPrefix(StrEnum):
    """Префиксы плейсхолдеров колонок: c_ — connections, r_ — roles, g_ — grants."""

    CONNECTIONS = "c_"
    ROLES = "r_"
    GRANTS = "g_"


class ConnectionNames:
    """Карты плейсхолдеров SQL таблиц соединений: имя в `{...}` → enum имени."""

    @staticmethod
    def tables() -> Mapping[str, ConnectionTable]:
        return {table.value: table for table in ConnectionTable}

    @classmethod
    def columns(cls) -> Mapping[str, StrEnum]:
        return dict(cls._columns())

    @staticmethod
    def _columns() -> Iterator[tuple[str, StrEnum]]:
        for column in ConnectionsColumn:
            yield ColumnPrefix.CONNECTIONS.value + column.value, column

        for column in RolesColumn:
            yield ColumnPrefix.ROLES.value + column.name.lower(), column

        for column in GrantsColumn:
            yield ColumnPrefix.GRANTS.value + column.value, column


class LikePattern(StrEnum):
    """Служебные символы ILIKE и сборка шаблона «содержит подстроку»."""

    ANY = "%"
    ONE = "_"
    ESCAPE = "\\"

    @classmethod
    def contains(cls, text: str) -> str:
        """Шаблон ILIKE для подстроки: спецсимволы текста экранированы."""
        escaped = text
        for special in (cls.ESCAPE, cls.ANY, cls.ONE):
            escaped = escaped.replace(special.value, cls.ESCAPE.value + special.value)

        return cls.ANY.value + escaped + cls.ANY.value


class FilterParam(StrEnum):
    """Имена параметров запроса, которые выставляют фильтры."""

    KIND = "kind"
    NAME = "name_pattern"
    HOST = "host_pattern"
    DESCRIPTION = "description_patterns"


class ConnectionFilter(BaseModel):
    """Отбор строк субъекта; пустое поле в запрос не попадает, заданные
    складываются по И. Условия применяются к строке c таблицы connections
    до выдачи, дубли считаются по всем строкам субъекта."""

    model_config = ConfigDict(frozen=True)

    kind: str = ""
    name: str = ""
    host: str = ""
    description: str = ""
    unique_only: bool = False

    KIND_CLAUSE: ClassVar[LiteralString] = (
        "and lower(c.{c_data} ->> %(kind_key)s) = %(kind)s"
    )
    NAME_CLAUSE: ClassVar[LiteralString] = "and c.{c_name} ilike %(name_pattern)s"
    HOST_CLAUSE: ClassVar[LiteralString] = (
        "and coalesce(c.{c_data} ->> %(host_key)s, '') ilike %(host_pattern)s"
    )
    DESCRIPTION_CLAUSE: ClassVar[LiteralString] = (
        "and coalesce(c.{c_data} ->> %(description_key)s, '') "
        "ilike all(%(description_patterns)s)"
    )
    UNIQUE_CLAUSE: ClassVar[LiteralString] = "and cp.copies = 1"

    @classmethod
    def none(cls) -> ConnectionFilter:
        return cls()

    @classmethod
    def of_kind(cls, kind: str) -> ConnectionFilter:
        return cls(kind=kind)

    @property
    def empty(self) -> bool:
        return self == self.none()

    def clauses(self) -> LiteralString:
        """Условия where по заданным полям; без фильтров — пустой текст."""
        return "\n".join(self._clauses())

    def params(self) -> dict[str, Any]:
        """Параметры условий: только по заданным полям."""
        return dict(self._params())

    def _clauses(self) -> Iterator[LiteralString]:
        if self.kind.strip():
            yield self.KIND_CLAUSE

        if self.name.strip():
            yield self.NAME_CLAUSE

        if self.host.strip():
            yield self.HOST_CLAUSE

        if self.description.split():
            yield self.DESCRIPTION_CLAUSE

        if self.unique_only:
            yield self.UNIQUE_CLAUSE

    def _params(self) -> Iterator[tuple[str, Any]]:
        if self.kind.strip():
            yield FilterParam.KIND.value, self.kind.strip().lower()

        if self.name.strip():
            yield FilterParam.NAME.value, LikePattern.contains(self.name.strip())

        if self.host.strip():
            yield FilterParam.HOST.value, LikePattern.contains(self.host.strip())

        if self.description.split():
            yield FilterParam.DESCRIPTION.value, list(self._word_patterns())

    def _word_patterns(self) -> Iterator[str]:
        for word in self.description.split():
            yield LikePattern.contains(word)


class SubjectGrantsQuery:
    """Строки connections, выданные субъекту лично или любой его роли.

    Выдача — колонки SubjectRowColumn: id, name, data, kind и copies — сколько
    строк субъекта носят это имя внутри вида (copies > 1 — дубль). Дубли
    считаются по всем строкам субъекта, фильтры отбирают строки из них.
    """

    TEXT: ClassVar[LiteralString] = """
        with
        subject_roles as (
            select
                r.{r_id}
            from
                {roles} r
            where
                r.{r_role} = any(%(roles)s)
        ),
        granted as (
            select
                g.{g_src_kind_id} as connection_id
            from
                {grants} g
            where 1=1
                and g.{g_src_kind} = %(src_kind)s
                and g.{g_tgt_kind} = %(users_kind)s
                and g.{g_tgt_kind_id} = %(user_id)s
            union
            select
                g.{g_src_kind_id} as connection_id
            from
                {grants} g
                inner join subject_roles sr on g.{g_tgt_kind_id} = sr.{r_id}
            where 1=1
                and g.{g_src_kind} = %(src_kind)s
                and g.{g_tgt_kind} = %(roles_kind)s
        ),
        copies as (
            select
                c.{c_name} as name,
                c.{c_data} ->> %(kind_key)s as kind,
                count(*) as copies
            from
                {connections} c
                inner join granted on granted.connection_id = c.{c_id}
            group by
                c.{c_name},
                c.{c_data} ->> %(kind_key)s
        )
        select
            c.{c_id} as id,
            c.{c_name} as name,
            c.{c_data} as data,
            cp.kind as kind,
            cp.copies as copies
        from
            {connections} c
            inner join granted on granted.connection_id = c.{c_id}
            inner join copies cp on 1=1
                and cp.name = c.{c_name}
                and cp.kind is not distinct from c.{c_data} ->> %(kind_key)s
        where 1=1
            {filters}
        order by
            cp.kind,
            c.{c_name}
    """

    FILTERS: ClassVar[LiteralString] = "{filters}"

    @classmethod
    def text(cls, flt: ConnectionFilter) -> LiteralString:
        """Текст запроса с условиями фильтра на месте {filters}."""
        return cls.TEXT.replace(cls.FILTERS, flt.clauses())

    @staticmethod
    def params(subject: Subject, flt: ConnectionFilter) -> dict[str, Any]:
        params: dict[str, Any] = {
            "src_kind": GrantKind.CONNECTIONS.value,
            "users_kind": GrantKind.USERS.value,
            "roles_kind": GrantKind.ROLES.value,
            "user_id": subject.user_id,
            "roles": sorted(subject.roles),
            "kind_key": ProfileKey.KIND.value,
            "host_key": ProfileKey.HOST.value,
            "description_key": ProfileKey.DESCRIPTION.value,
        }
        params.update(flt.params())

        return params
