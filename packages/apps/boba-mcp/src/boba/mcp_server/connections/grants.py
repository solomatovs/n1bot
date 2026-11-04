"""SQL выдачи доступных connections и фильтров каталога.

Ошибки: своих не выпускает; SQL исполняет ConnectionStore."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, LiteralString

from boba.access.grants import ConnectionFilter, GrantResource
from boba.access_postgres.grants import GrantedIds
from boba.identity.context import Subject


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


class LikeSpecial(StrEnum):
    ANY = "%"
    ONE = "_"
    ESCAPE = "\\"


@dataclass(frozen=True)
class LikePattern:
    """Подстрока фильтра, экранированная для PostgreSQL ILIKE."""

    text: str

    def contains(self) -> str:
        escaped = self.text
        for special in (LikeSpecial.ESCAPE, LikeSpecial.ANY, LikeSpecial.ONE):
            escaped = escaped.replace(special, LikeSpecial.ESCAPE + special)

        return LikeSpecial.ANY + escaped + LikeSpecial.ANY


class FilterParam(StrEnum):
    """Имена параметров запроса, которые выставляют фильтры."""

    KIND = "kind"
    NAME = "name_pattern"
    HOST = "host_pattern"
    DESCRIPTION = "description_patterns"


class FilterClause(StrEnum):
    KIND_CLAUSE = "and lower(c.data ->> %(kind_key)s) = %(kind)s"
    NAME_CLAUSE = "and c.name ilike %(name_pattern)s"
    HOST_CLAUSE = "and coalesce(c.data ->> %(host_key)s, '') ilike %(host_pattern)s"
    DESCRIPTION_CLAUSE = (
        "and coalesce(c.data ->> %(description_key)s, '') "
        "ilike all(%(description_patterns)s)"
    )
    UNIQUE_CLAUSE = "and cp.copies = 1"


class SubjectGrantsQuery:
    """Строки connections, выданные субъекту лично или любой его роли.

    Выдача — колонки SubjectRowColumn: id, name, data, kind и copies — сколько
    строк субъекта носят это имя внутри вида (copies > 1 — дубль). Дубли
    считаются по всем строкам субъекта, фильтры отбирают строки из них.
    Исполнитель подставляет схему таблиц именем {schema} и биндит params().
    """

    TEXT: ClassVar[LiteralString] = (
        GrantedIds.TEXT
        + """
        , copies as (
            select
                c.name as name,
                c.data ->> %(kind_key)s as kind,
                count(*) as copies
            from
                {schema}.connections c
                inner join granted on granted.src_id = c.id
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
            inner join granted on granted.src_id = c.id
            inner join copies cp on 1=1
                and cp.name = c.name
                and cp.kind is not distinct from c.data ->> %(kind_key)s
        where 1=1
            {filters}
        order by
            cp.kind,
            c.name
    """
    )

    def __init__(self, resource: GrantResource) -> None:
        self._grants = GrantedIds()
        self._resource = resource

    def text(self, flt: ConnectionFilter, *, name: str | None = None) -> LiteralString:
        clauses = "\n".join(self._clauses(flt))
        if name is not None:
            clauses += "\nand c.name = %(connection_name)s"

        return self.TEXT.replace("{filters}", clauses)

    def params(
        self, subject: Subject, flt: ConnectionFilter, *, name: str | None = None
    ) -> dict[str, Any]:
        params = self._grants.params(subject.user_id, subject.roles, self._resource)
        params.update(
            {
                "kind_key": ProfileKey.KIND.value,
                "host_key": ProfileKey.HOST.value,
                "description_key": ProfileKey.DESCRIPTION.value,
            }
        )
        params.update(self._filter_params(flt))
        if name is not None:
            params["connection_name"] = name

        return params

    def _clauses(self, flt: ConnectionFilter) -> Iterator[LiteralString]:
        if flt.kind.strip():
            yield FilterClause.KIND_CLAUSE.value

        if flt.name.strip():
            yield FilterClause.NAME_CLAUSE.value

        if flt.host.strip():
            yield FilterClause.HOST_CLAUSE.value

        if flt.description.split():
            yield FilterClause.DESCRIPTION_CLAUSE.value

        if flt.unique_only:
            yield FilterClause.UNIQUE_CLAUSE.value

    def _filter_params(self, flt: ConnectionFilter) -> Iterator[tuple[str, Any]]:
        if flt.kind.strip():
            yield FilterParam.KIND.value, flt.kind.strip().lower()

        if flt.name.strip():
            yield FilterParam.NAME.value, LikePattern(flt.name.strip()).contains()

        if flt.host.strip():
            yield FilterParam.HOST.value, LikePattern(flt.host.strip()).contains()

        if flt.description.split():
            yield (
                FilterParam.DESCRIPTION.value,
                list(self._word_patterns(flt.description)),
            )

    def _word_patterns(self, description: str) -> Iterator[str]:
        for word in description.split():
            yield LikePattern(word).contains()
