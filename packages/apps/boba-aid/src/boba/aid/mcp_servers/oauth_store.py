"""Зашифрованное хранилище OAuth и адаптер TokenStorage для MCP SDK.

Ошибки:
McpOAuthStoreError — чтение, запись, шифрование, разбор данных или поколение
    учётных данных не позволяют выполнить операцию; причина сохранена.
CancelledError — отмена ожидания блокировки или запроса распространяется без упаковки.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg import sql
from pydantic import SecretStr, ValidationError

from boba.crypto import SecretCipher
from boba.db.postgres import PostgresPool, PostgresTable


class McpOAuthStoreError(Exception):
    """OAuth credentials could not be read or persisted."""


@dataclass
class OAuthWriteGate:
    """Per-account lock and generation for revoking in-flight token writers."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0


class OAuthField(StrEnum):
    TOKEN = "token_data"  # noqa: S105 — database column name
    CLIENT = "client_data"


class McpOAuthStore(PostgresTable):
    """One encrypted token and DCR record per server and user."""

    LABEL = "mcp_tokens"

    def __init__(self, pool: PostgresPool, schema: str, cipher: SecretCipher) -> None:
        try:
            super().__init__(pool, schema)
            self._cipher = cipher
        except Exception as exc:
            raise McpOAuthStoreError(
                f"Configuring MCP OAuth storage in schema {schema!r} failed: {exc}"
            ) from exc

    def _failure(self, action: str, exc: Exception) -> McpOAuthStoreError:
        return McpOAuthStoreError(self._detail(action, exc))

    @contextmanager
    def _operation(self, action: str, server_id: UUID, user_id: UUID) -> Iterator[None]:
        try:
            yield
        except Exception as exc:
            raise McpOAuthStoreError(
                f"MCP OAuth storage {self.schema}: {action} for server "
                f"{server_id}, user {user_id} failed: {exc}"
            ) from exc

    async def get_tokens(self, server_id: UUID, user_id: UUID) -> OAuthToken | None:
        with self._operation("reading tokens", server_id, user_id):
            raw = await self._get(server_id, user_id, OAuthField.TOKEN)
            if raw is None:
                return None
            try:
                return OAuthToken.model_validate_json(raw)
            except ValidationError as exc:
                raise McpOAuthStoreError(
                    "stored MCP OAuth token has invalid fields"
                ) from exc

    async def set_tokens(
        self, server_id: UUID, user_id: UUID, tokens: OAuthToken
    ) -> None:
        with self._operation("saving tokens", server_id, user_id):
            await self._put(
                server_id, user_id, OAuthField.TOKEN, tokens.model_dump_json()
            )

    async def get_client_info(
        self, server_id: UUID, user_id: UUID
    ) -> OAuthClientInformationFull | None:
        with self._operation("reading client registration", server_id, user_id):
            raw = await self._get(server_id, user_id, OAuthField.CLIENT)
            if raw is None:
                return None
            try:
                return OAuthClientInformationFull.model_validate_json(raw)
            except ValidationError as exc:
                raise McpOAuthStoreError(
                    "stored MCP OAuth client has invalid fields"
                ) from exc

    async def set_client_info(
        self, server_id: UUID, user_id: UUID, client: OAuthClientInformationFull
    ) -> None:
        with self._operation("saving client registration", server_id, user_id):
            await self._put(
                server_id, user_id, OAuthField.CLIENT, client.model_dump_json()
            )

    async def delete(self, server_id: UUID, user_id: UUID) -> None:
        with self._operation("deleting credentials", server_id, user_id):
            query = (
                self._query()
                .add(
                    "delete from {schema}.mcp_tokens "
                    "where server_id = %(server_id)s and user_id = %(user_id)s",
                    server_id=server_id,
                    user_id=user_id,
                )
                .build()
            )
            await self._execute(query, "disconnecting MCP OAuth")

    async def _get(
        self, server_id: UUID, user_id: UUID, column: OAuthField
    ) -> str | None:
        query = (
            self._query()
            .add(
                "select {field} from {schema}.mcp_tokens "
                "where server_id = %(server_id)s and user_id = %(user_id)s",
                field=sql.Identifier(column.value),
                server_id=server_id,
                user_id=user_id,
            )
            .build()
        )
        row = await self._row(query, f"reading {column.value} for MCP OAuth")
        if row is None:
            return None

        if row[column.value] is None:
            return None

        value = self._cipher.decrypt(row[column.value])
        if not isinstance(value, str):
            raise McpOAuthStoreError("stored MCP OAuth credential is not text")
        return value

    async def _put(
        self, server_id: UUID, user_id: UUID, column: OAuthField, value: str
    ) -> None:
        encrypted = self._cipher.encrypt(SecretStr(value))
        query = (
            self._query()
            .add(
                "insert into {schema}.mcp_tokens (server_id, user_id, {field}) "
                "values (%(server_id)s, %(user_id)s, %(value)s) "
                "on conflict (server_id, user_id) do update set "
                "{field} = excluded.{field}, updated_at = now()",
                field=sql.Identifier(column.value),
                server_id=server_id,
                user_id=user_id,
                value=encrypted,
            )
            .build()
        )
        await self._execute(query, f"saving {column.value} for MCP OAuth")


class UserOAuthTokens(TokenStorage):
    """The MCP SDK TokenStorage adapter for one server and one AID user."""

    def __init__(
        self,
        store: McpOAuthStore,
        server_id: UUID,
        user_id: UUID,
        gate: OAuthWriteGate,
    ) -> None:
        self._store = store
        self._server_id = server_id
        self._user_id = user_id
        self._gate = gate
        self._generation = gate.generation

    @contextmanager
    def _operation(self, action: str) -> Iterator[None]:
        try:
            yield
        except McpOAuthStoreError:
            raise
        except Exception as exc:
            raise McpOAuthStoreError(
                f"MCP OAuth adapter: {action} for server {self._server_id}, "
                f"user {self._user_id} failed: {exc}"
            ) from exc

    async def get_tokens(self) -> OAuthToken | None:
        with self._operation("reading tokens"):
            async with self._gate.lock:
                self._active()
                return await self._store.get_tokens(self._server_id, self._user_id)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        with self._operation("saving tokens"):
            async with self._gate.lock:
                self._active()
                await self._store.set_tokens(self._server_id, self._user_id, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        with self._operation("reading client registration"):
            async with self._gate.lock:
                self._active()
                return await self._store.get_client_info(self._server_id, self._user_id)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        with self._operation("saving client registration"):
            async with self._gate.lock:
                self._active()
                await self._store.set_client_info(
                    self._server_id, self._user_id, client_info
                )

    def _active(self) -> None:
        if self._generation != self._gate.generation:
            raise McpOAuthStoreError("MCP OAuth credentials were disconnected")
