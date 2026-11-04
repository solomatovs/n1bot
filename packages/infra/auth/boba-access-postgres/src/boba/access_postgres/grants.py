"""Общий CTE выдачи ресурса пользователю и его ролям.

Ошибки: своих не выпускает; запрос исполняет репозиторий ресурса."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar, LiteralString
from uuid import UUID

from boba.access.grants import GrantResource, GrantSubject


class GrantedIds:
    """Идентификаторы строк одной таблицы-источника, выданные субъекту лично
    или любой его роли, из таблицы grants.

    CTE subject_roles и granted находят гранты переданного вида ресурса.
    Текст собирается частями литералов:
    сначала GrantedIds.TEXT, затем своя проекция и фильтры хозяина.
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
                g.src_kind_id as src_id
            from
                {schema}.grants g
            where 1=1
                and g.src_kind = %(src_kind)s
                and g.tgt_kind = %(users_kind)s
                and g.tgt_kind_id = %(user_id)s
            union
            select
                g.src_kind_id as src_id
            from
                {schema}.grants g
                inner join subject_roles sr on g.tgt_kind_id = sr.id
            where 1=1
                and g.src_kind = %(src_kind)s
                and g.tgt_kind = %(roles_kind)s
        )
    """

    def params(
        self, user_id: UUID, roles: Iterable[str], src_kind: GrantResource
    ) -> dict[str, Any]:
        """Параметры CTE: src_kind задаёт вид выдачи, user_id и roles — кому
        она считается."""
        return {
            "src_kind": src_kind.value,
            "users_kind": GrantSubject.USERS.value,
            "roles_kind": GrantSubject.ROLES.value,
            "user_id": user_id,
            "roles": sorted(roles),
        }
