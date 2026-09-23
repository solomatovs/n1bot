"""Гранты соединений субъекту: один SQL, по которому хост и инструменты
узнают, какие строки connections выданы пользователю лично или любой его роли.

Здесь же, где решается доступ к инструментам, живёт и доступ к соединениям:
текст запроса один, чтобы брокер (выбор строки под вызов) и плагин
connection_list/connection_search (каталог для модели) считали выдачу и дубли
имён одинаково — имя, выданное дважды внутри одного вида, дубль, вызов по
нему неоднозначен. Домен psycopg не знает: текст несёт имя схемы
плейсхолдером `{schema}` и параметры `%(...)s`, исполнитель собирает его
своим сборщиком запросов.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Any, ClassVar, LiteralString

from pydantic import BaseModel, ConfigDict

from boba.connections.stored import GrantKind
from boba.identity.context import Subject

__all__ = [
    "ConnectionFilter",
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
        "and lower(c.data ->> %(kind_key)s) = %(kind)s"
    )
    NAME_CLAUSE: ClassVar[LiteralString] = "and c.name ilike %(name_pattern)s"
    HOST_CLAUSE: ClassVar[LiteralString] = (
        "and coalesce(c.data ->> %(host_key)s, '') ilike %(host_pattern)s"
    )
    DESCRIPTION_CLAUSE: ClassVar[LiteralString] = (
        "and coalesce(c.data ->> %(description_key)s, '') "
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
    Исполнитель подставляет схему таблиц именем {schema} и биндит params().
    """

    TEXT: ClassVar[LiteralString] = """
        with
        subject_roles as (
            select
                r.id
            from
                {schema}.roles r
            where
                r.role = any(%(roles)s)
        ),
        granted as (
            select
                g.src_kind_id as connection_id
            from
                {schema}.grants g
            where 1=1
                and g.src_kind = %(src_kind)s
                and g.tgt_kind = %(users_kind)s
                and g.tgt_kind_id = %(user_id)s
            union
            select
                g.src_kind_id as connection_id
            from
                {schema}.grants g
                inner join subject_roles sr on g.tgt_kind_id = sr.id
            where 1=1
                and g.src_kind = %(src_kind)s
                and g.tgt_kind = %(roles_kind)s
        ),
        copies as (
            select
                c.name as name,
                c.data ->> %(kind_key)s as kind,
                count(*) as copies
            from
                {schema}.connections c
                inner join granted on granted.connection_id = c.id
            group by
                c.name,
                c.data ->> %(kind_key)s
        )
        select
            c.id as id,
            c.name as name,
            c.data as data,
            cp.kind as kind,
            cp.copies as copies
        from
            {schema}.connections c
            inner join granted on granted.connection_id = c.id
            inner join copies cp on 1=1
                and cp.name = c.name
                and cp.kind is not distinct from c.data ->> %(kind_key)s
        where 1=1
            {filters}
        order by
            cp.kind,
            c.name
    """

    FILTERS: ClassVar[LiteralString] = "{filters}"

    def __init__(self, subject: Subject, flt: ConnectionFilter) -> None:
        self._subject = subject
        self._filter = flt

    def text(self) -> LiteralString:
        """Текст запроса с условиями фильтра на месте {filters}."""
        return self.TEXT.replace(self.FILTERS, self._filter.clauses())

    def params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "src_kind": GrantKind.CONNECTIONS.value,
            "users_kind": GrantKind.USERS.value,
            "roles_kind": GrantKind.ROLES.value,
            "user_id": self._subject.user_id,
            "roles": sorted(self._subject.roles),
            "kind_key": ProfileKey.KIND.value,
            "host_key": ProfileKey.HOST.value,
            "description_key": ProfileKey.DESCRIPTION.value,
        }
        params.update(self._filter.params())

        return params
