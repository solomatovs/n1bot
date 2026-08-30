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
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Json
from pydantic import BaseModel

from boba.connections.postgres import PostgresConfig
from boba.db.postgres import AsyncPostgresPool, PostgresError, SqlNames
from boba.identity.context import Scope
from boba.messaging import PayloadMissingError, PayloadRef, PayloadStore
from boba.messaging.bus import LivePayloadsColumn, LiveTable
from boba.runtime.bus import ScopeKindCheck

__all__ = ["PayloadBody", "PayloadStoreError", "PgPayloadStore"]

logger = logging.getLogger(__name__)


class PayloadStoreError(Exception):
    """База тел недоступна или запрос не выполнен."""


class PayloadBody:
    """Приводит тело к JSON перед записью: модель — дампом, строку и словарь — как
    есть, объект с полем content — его содержимым, остальное — строкой.
    """

    @classmethod
    def of(cls, payload: object) -> Any:
        if isinstance(payload, BaseModel):
            return payload.model_dump(mode="json")

        if isinstance(payload, str):
            return payload

        if isinstance(payload, Mapping):
            return json.loads(json.dumps(dict(payload), default=str))

        content = getattr(payload, "content", None)
        if content is not None:
            return cls.of(content)

        return str(payload)


class PgPayloadStore(PayloadStore):
    """Кладёт тела сообщений в live_payloads и отдаёт их по ссылке; ссылка хранит
    область и uuid строки. Колонка body — json, а не jsonb: jsonb переупорядочивает
    ключи, а порядок аргументов и колонок результата виден пользователю.
    """

    def __init__(self, cfg: PostgresConfig, db_schema: str) -> None:
        self._cfg = cfg
        self._schema = db_schema
        self._pool_ref: AsyncPostgresPool | None = None

    async def _pool(self) -> AsyncPostgresPool:
        if self._pool_ref is None:
            self._pool_ref = await AsyncPostgresPool.get(self._cfg)

        return self._pool_ref

    def _table(self) -> sql.Identifier:
        return SqlNames.table(self._schema, LiveTable.PAYLOADS)

    async def setup(self) -> None:
        """Создаёт live_payloads; схему готовит шина."""
        ddl = (
            sql.SQL(
                """
                create unlogged table if not exists {payloads} (
                    {scope_kind} text not null,
                    {scope_id}   uuid not null,
                    {id}         uuid primary key,
                    {body}       json not null,
                    {at}         timestamptz not null default now()
                )
                """
            ).format(
                payloads=self._table(),
                **{
                    column.value: SqlNames.ident(column)
                    for column in LivePayloadsColumn
                },
            ),
            sql.SQL(
                """
                alter table {payloads}
                    alter column {body} type json using {body}::text::json
                """
            ).format(
                payloads=self._table(), body=SqlNames.ident(LivePayloadsColumn.BODY)
            ),
            sql.SQL(
                """
                create index if not exists idx_live_payloads_scope
                on {payloads} ({scope_kind}, {scope_id})
                """
            ).format(
                payloads=self._table(),
                scope_kind=SqlNames.ident(LivePayloadsColumn.SCOPE_KIND),
                scope_id=SqlNames.ident(LivePayloadsColumn.SCOPE_ID),
            ),
            ScopeKindCheck.of(self._schema, LiveTable.PAYLOADS),
        )
        pool = await self._pool()
        try:
            async with pool.connection() as conn, conn.transaction():
                for statement in ddl:
                    await conn.execute(statement, prepare=False)
        except (psycopg.Error, PostgresError) as exc:
            msg = "payload store: setup failed"
            raise PayloadStoreError(msg) from exc

    @staticmethod
    def _scope_id(scope: Scope) -> UUID:
        try:
            return UUID(scope.id)
        except ValueError as exc:
            msg = f"scope id is not a uuid: {scope.id!r}"
            raise PayloadStoreError(msg) from exc

    async def put(self, scope: Scope, payload: object) -> PayloadRef:
        ref = PayloadRef(scope=scope, id=uuid4().hex)
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        insert into {payloads} ({scope_kind}, {scope_id}, {id}, {body})
                        values (%(scope_kind)s, %(scope_id)s, %(id)s, %(body)s)
                        """
                    ).format(
                        payloads=self._table(),
                        scope_kind=SqlNames.ident(LivePayloadsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LivePayloadsColumn.SCOPE_ID),
                        id=SqlNames.ident(LivePayloadsColumn.ID),
                        body=SqlNames.ident(LivePayloadsColumn.BODY),
                    ),
                    {
                        "scope_kind": scope.kind.value,
                        "scope_id": self._scope_id(scope),
                        "id": UUID(ref.id),
                        "body": Json(PayloadBody.of(payload)),
                    },
                    prepare=False,
                )
        except (psycopg.Error, PostgresError) as exc:
            msg = f"payloads: put into {scope.render()} failed"
            raise PayloadStoreError(msg) from exc

        return ref

    async def get(self, ref: PayloadRef) -> object:
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        select {body} from {payloads}
                        where 1=1
                            and {id} = %(id)s
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                        """
                    ).format(
                        payloads=self._table(),
                        body=SqlNames.ident(LivePayloadsColumn.BODY),
                        id=SqlNames.ident(LivePayloadsColumn.ID),
                        scope_kind=SqlNames.ident(LivePayloadsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LivePayloadsColumn.SCOPE_ID),
                    ),
                    {
                        "id": UUID(ref.id),
                        "scope_kind": ref.scope.kind.value,
                        "scope_id": self._scope_id(ref.scope),
                    },
                    prepare=False,
                )
                row = await cur.fetchone()
        except (psycopg.Error, PostgresError) as exc:
            msg = f"payloads: get of {ref.id} failed"
            raise PayloadStoreError(msg) from exc

        if row is None:
            msg = f"payload {ref.id} of {ref.scope.render()} is gone"
            raise PayloadMissingError(msg)

        return row[0]

    async def purge(self, scope: Scope) -> int:
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        delete from {payloads}
                        where 1=1
                            and {scope_kind} = %(scope_kind)s
                            and {scope_id} = %(scope_id)s
                        """
                    ).format(
                        payloads=self._table(),
                        scope_kind=SqlNames.ident(LivePayloadsColumn.SCOPE_KIND),
                        scope_id=SqlNames.ident(LivePayloadsColumn.SCOPE_ID),
                    ),
                    {"scope_kind": scope.kind.value, "scope_id": self._scope_id(scope)},
                    prepare=False,
                )
                return cur.rowcount
        except (psycopg.Error, PostgresError) as exc:
            msg = f"payloads: purge of {scope.render()} failed"
            raise PayloadStoreError(msg) from exc

    async def purge_idle(self, max_age_sec: int) -> int:
        """Удаляет тела старше max_age_sec, потому что их сообщения уже никто не
        читает; возвращает число удалённых.
        """
        pool = await self._pool()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        """
                        delete from {payloads}
                        where
                            {at} + make_interval(secs => %(age)s) < now()
                        """
                    ).format(
                        payloads=self._table(), at=SqlNames.ident(LivePayloadsColumn.AT)
                    ),
                    {"age": max_age_sec},
                    prepare=False,
                )
                return cur.rowcount
        except (psycopg.Error, PostgresError) as exc:
            msg = "payloads: purge of idle bodies failed"
            raise PayloadStoreError(msg) from exc
