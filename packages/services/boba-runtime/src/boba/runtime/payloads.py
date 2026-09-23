"""Хранит тела сообщений шины в таблице live_payloads, чтобы получатель на любом
инстансе мог забрать их по ссылке из сообщения.

Ошибки:
PayloadMissingError — тела по ссылке нет.
PayloadStoreError — база недоступна или запрос не выполнен.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, ClassVar
from uuid import UUID, uuid4

from psycopg.types.json import Json
from pydantic import BaseModel

from boba.db.postgres import PgQuery, PostgresPool, PostgresTable
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import Scope
from boba.messaging import PayloadMissingError, PayloadRef, PayloadStore
from boba.messaging.bus import LivePayloadsColumn, LiveTable
from boba.runtime.bus import ScopeKindCheck

__all__ = ["PayloadBody", "PayloadStoreError", "PgPayloadStore"]

logger = logging.getLogger(__name__)


class PayloadStoreError(Exception):
    """База тел недоступна или запрос не выполнен."""


class PayloadBody:
    """Тело сообщения в форме JSON перед записью: модель — дампом, строка и
    словарь — как есть, объект с полем content — его содержимым, остальное —
    строкой.
    """

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def render(self) -> Any:
        payload = self._payload
        if isinstance(payload, BaseModel):
            return payload.model_dump(mode="json")

        if isinstance(payload, str):
            return payload

        if isinstance(payload, Mapping):
            return json.loads(json.dumps(dict(payload), default=str))

        content = getattr(payload, "content", None)
        if content is not None:
            return PayloadBody(content).render()

        return str(payload)


class PgPayloadStore(PostgresTable, PayloadStore):
    """Кладёт тела сообщений в live_payloads и отдаёт их по ссылке; ссылка хранит
    область и uuid строки. Колонка body — json, а не jsonb: jsonb переупорядочивает
    ключи, а порядок аргументов и колонок результата виден пользователю.
    """

    LABEL: ClassVar[str] = "payloads"

    def __init__(
        self, cfg: PostgresConfig, db_schema: str, pool: PostgresPool | None = None
    ) -> None:
        super().__init__(cfg, db_schema, pool)
        self._scope_kind_check = ScopeKindCheck(db_schema)

    def _failure(self, action: str, exc: Exception) -> Exception:
        return PayloadStoreError(self._detail(action, exc))

    def _scope_id(self, scope: Scope) -> UUID:
        try:
            return scope.uuid()
        except ValueError as exc:
            raise PayloadStoreError(f"payloads: {exc}") from exc

    async def setup(self) -> None:
        """Создаёт live_payloads; схему готовит шина."""
        ddl: tuple[PgQuery, ...] = (
            self._query()
            .add(
                """
                create unlogged table if not exists {schema}.live_payloads (
                    scope_kind text not null,
                    scope_id   uuid not null,
                    id         uuid primary key,
                    body       json not null,
                    at         timestamptz not null default now()
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                alter table {schema}.live_payloads
                    alter column body type json using body::text::json
                """
            )
            .build(),
            self._query()
            .add(
                """
                create index if not exists idx_live_payloads_scope
                on {schema}.live_payloads (scope_kind, scope_id)
                """
            )
            .build(),
            self._scope_kind_check.of(LiveTable.PAYLOADS),
        )

        await self._apply_ddl(ddl)

    async def put(self, scope: Scope, payload: object) -> PayloadRef:
        ref = PayloadRef(scope=scope, id=uuid4().hex)
        query = (
            self._query()
            .add(
                """
                insert into {schema}.live_payloads (scope_kind, scope_id, id, body)
                values (%(scope_kind)s, %(scope_id)s, %(id)s, %(body)s)
                """,
                scope_kind=scope.kind.value,
                scope_id=self._scope_id(scope),
                id=UUID(ref.id),
                body=Json(PayloadBody(payload).render()),
            )
            .build()
        )

        await self._execute(
            query, f"insert of {ref.id} for {scope.render()} into live_payloads"
        )

        return ref

    async def get(self, ref: PayloadRef) -> object:
        query = (
            self._query()
            .add(
                """
                select body from {schema}.live_payloads
                where 1=1
                    and id = %(id)s
                    and scope_kind = %(scope_kind)s
                    and scope_id = %(scope_id)s
                """,
                id=UUID(ref.id),
                scope_kind=ref.scope.kind.value,
                scope_id=self._scope_id(ref.scope),
            )
            .build()
        )
        row = await self._row(
            query, f"reading {ref.id} for {ref.scope.render()} in live_payloads"
        )

        if row is None:
            msg = (
                f"payload {ref.id} of {ref.scope.render()} is gone: no row in "
                f"{self.schema}.live_payloads"
            )
            raise PayloadMissingError(msg)

        return row[LivePayloadsColumn.BODY.value]

    async def purge(self, scope: Scope) -> int:
        query = (
            self._query()
            .add(
                """
                delete from {schema}.live_payloads
                where 1=1
                    and scope_kind = %(scope_kind)s
                    and scope_id = %(scope_id)s
                """,
                scope_kind=scope.kind.value,
                scope_id=self._scope_id(scope),
            )
            .build()
        )

        return await self._execute(
            query, f"delete of {scope.render()} from live_payloads"
        )

    async def purge_idle(self, max_age_sec: int) -> int:
        """Удаляет тела старше max_age_sec, потому что их сообщения уже никто не
        читает; возвращает число удалённых.
        """
        query = (
            self._query()
            .add(
                """
                delete from {schema}.live_payloads
                where
                    at + make_interval(secs => %(age)s) < now()
                """,
                age=max_age_sec,
            )
            .build()
        )

        return await self._execute(
            query, f"delete of bodies older than {max_age_sec}s from live_payloads"
        )
