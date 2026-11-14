"""PostgreSQL-история и запуск SQL-адаптеров.

Преобразование содержимого выполняет SQL.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from boba.agent.acp_validator import (
    AcpCommandsDraft,
    AcpElicitationDraft,
    AcpHistoryDraft,
    AcpHistoryValidator,
    AcpNoticeDraft,
    AcpOptionsDraft,
    AcpPermissionDraft,
    AcpStateDraft,
)
from boba.agent.models import (
    HISTORY_RECORD_ADAPTER,
    History,
    HistoryRecord,
    HistoryWriteError,
)
from boba.agent.openai_validator import OpenAiRequestDraft, OpenAiRequestValidator
from boba.agent.queries import Query
from boba.db.postgres import AsyncPostgresPool


class HistoryStore:
    """Единственное хранилище агента. Подтверждение записи возвращается после commit."""

    def __init__(self, pool: AsyncPostgresPool) -> None:
        self.pool = pool

    @asynccontextmanager
    async def transaction(
        self, session_id: str
    ) -> AsyncGenerator[HistoryTransaction, None]:
        """Короткая транзакция записи одной сессии; HTTP/MCP внутри не выполняются."""
        if not session_id:
            raise ValueError("session_id must not be empty")
        batch = None
        committing = False
        try:
            async with self.pool.connection() as connection, connection.transaction():
                batch = HistoryTransaction(connection, session_id)
                await batch.rows(Query.LOCK, session_id)
                yield batch
                committing = True
        except psycopg.Error as exc:
            code = "commit_unknown" if committing else "storage_unavailable"
            if isinstance(exc, psycopg.IntegrityError):
                code = "record_conflict"
            raise HistoryWriteError(
                session_id=session_id,
                record_ids=tuple(batch.record_ids) if batch else (),
                code=code,
                message=f"History write failed: {code}",
            ) from exc

    async def append(
        self,
        session_id: str,
        records: Sequence[HistoryRecord],
        *,
        expected_seq: int | None = None,
    ) -> tuple[HistoryRecord, ...]:
        async with self.transaction(session_id) as transaction:
            saved = await transaction.append(records, expected_seq=expected_seq)
        return saved

    async def rows(self, query: Query, *params: Any) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            return await _rows(connection, query, params)

    async def read(
        self, session_id: str, *, after_seq: int = 0, through_seq: int | None = None
    ) -> History:
        rows = await self.rows(Query.READ, session_id, after_seq, through_seq)
        return History.model_validate({"session_id": session_id, "rows": rows})

    async def pending_inputs(self, session_id: str) -> tuple[HistoryRecord, ...]:
        rows = await self.rows(Query.PENDING_INPUTS, session_id, None)
        return tuple(HISTORY_RECORD_ADAPTER.validate_python(row) for row in rows)

    async def head(self, session_id: str) -> int:
        return (await self.rows(Query.HEAD, session_id))[0]["seq"]

    async def latest(
        self,
        session_id: str,
        *,
        category: str | None = None,
        kind: str | Sequence[str] | None = None,
        key: str | None = None,
        through_seq: int | None = None,
    ) -> tuple[HistoryRecord, ...]:
        kinds = None
        if isinstance(kind, str):
            kinds = [kind]
        elif kind is not None:
            kinds = list(kind)

        rows = await self.rows(
            Query.LATEST, session_id, category, kinds, key, through_seq
        )
        return tuple(HISTORY_RECORD_ADAPTER.validate_python(row) for row in rows)

    async def create_session(
        self,
        session_id: str,
        owner_id: str,
        cwd: str,
        records: Sequence[HistoryRecord],
    ) -> None:
        async with self.transaction(session_id) as transaction:
            await transaction.rows(Query.SESSION_CREATE, session_id, owner_id, cwd)
            await transaction.append(records, expected_seq=0)

    async def session(self, session_id: str, owner_id: str) -> dict[str, Any]:
        rows = await self.rows(Query.SESSION_GET, session_id, owner_id)
        if not rows:
            raise LookupError("Session not found")
        return rows[0]

    async def openai(
        self,
        session_id: str,
        request_id: str | None = None,
        *,
        through_seq: int | None = None,
    ) -> OpenAiRequestDraft:
        raw = (await self.rows(Query.OPENAI, session_id, request_id, through_seq))[0]
        draft = OpenAiRequestDraft.model_validate(raw)
        OpenAiRequestValidator().require_valid(draft)
        return draft

    async def acp(self, query: Query, *params: Any) -> dict[str, Any]:
        """Проверить SQL-результат и вернуть исходный JSON без повторной сборки."""
        validator = AcpHistoryValidator()
        contracts = {
            Query.ACP_HISTORY: (AcpHistoryDraft, validator.require_valid),
            Query.ACP_STATE: (AcpStateDraft, validator.require_state),
            Query.ACP_COMMANDS: (AcpCommandsDraft, validator.require_commands),
            Query.ACP_OPTIONS: (AcpOptionsDraft, validator.require_options),
            Query.ACP_NOTICE: (AcpNoticeDraft, AcpNoticeDraft.require_valid),
            Query.ACP_PERMISSION: (AcpPermissionDraft, validator.require_permission),
            Query.ACP_ELICITATION: (AcpElicitationDraft, validator.require_elicitation),
        }
        model, validate = contracts[query]
        raw = (await self.rows(query, *params))[0]
        validate(model.model_validate(raw))
        return raw


class HistoryTransaction:
    """Операции на одном соединении под блокировкой записи сессии."""

    def __init__(self, connection: psycopg.AsyncConnection, session_id: str) -> None:
        self.connection = connection
        self.session_id = session_id
        self.record_ids: list[str] = []

    async def rows(self, query: Query, *params: Any) -> list[dict[str, Any]]:
        return await _rows(self.connection, query, params)

    async def head(self) -> int:
        return (await self.rows(Query.HEAD, self.session_id))[0]["seq"]

    async def pending_inputs(self) -> tuple[HistoryRecord, ...]:
        rows = await self.rows(Query.PENDING_INPUTS, self.session_id, None)
        return tuple(HISTORY_RECORD_ADAPTER.validate_python(row) for row in rows)

    async def append(
        self, records: Sequence[HistoryRecord], *, expected_seq: int | None = None
    ) -> tuple[HistoryRecord, ...]:
        validated = tuple(
            HISTORY_RECORD_ADAPTER.validate_python(record) for record in records
        )
        if any(
            record.session_id is not None or record.seq is not None
            for record in validated
        ):
            raise ValueError("append accepts only records without session_id and seq")
        self.record_ids.extend(record.record_id for record in validated)
        # Generic JSON encoding only: the models already have the table's exact shape.
        payload = Jsonb([record.model_dump(mode="json") for record in validated])
        receipt = (
            await self.rows(Query.APPEND, self.session_id, payload, expected_seq)
        )[0]
        if receipt["error"]:
            raise self.failure(receipt["error"])
        saved = tuple(
            HISTORY_RECORD_ADAPTER.validate_python(row) for row in receipt["records"]
        )
        if not receipt["retry"]:
            issues = (
                await self.rows(
                    Query.HISTORY_INTEGRITY,
                    self.session_id,
                    saved[0].seq,
                    saved[-1].seq,
                )
            )[0]["errors"]
            if issues:
                raise self.failure("invalid_reference")
        return saved

    def failure(self, code: str) -> HistoryWriteError:
        return HistoryWriteError(
            session_id=self.session_id,
            record_ids=tuple(self.record_ids),
            code=code,
            message=f"History write rejected: {code}",
        )


async def _rows(connection, query: Query, params) -> list[dict[str, Any]]:
    async with psycopg.AsyncRawCursor(connection, row_factory=dict_row) as cursor:
        await cursor.execute(query.text(), params)
        return await cursor.fetchall()
