"""Persistent encrypted OAuth token and client registration storage.

Errors:
McpOAuthStoreError — PostgreSQL or decryption rejected a read or write.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg import sql
from pydantic import SecretStr, ValidationError

from boba.connection_broker.crypto import SecretCipher, SecretCryptoError
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
        super().__init__(None, schema, pool)
        self._cipher = cipher

    def _failure(self, action: str, exc: Exception) -> Exception:
        return McpOAuthStoreError(self._detail(action, exc))

    async def get_tokens(self, server_id: UUID, user_id: UUID) -> OAuthToken | None:
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
        await self._put(server_id, user_id, OAuthField.TOKEN, tokens.model_dump_json())

    async def get_client_info(
        self, server_id: UUID, user_id: UUID
    ) -> OAuthClientInformationFull | None:
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
        await self._put(server_id, user_id, OAuthField.CLIENT, client.model_dump_json())

    async def delete(self, server_id: UUID, user_id: UUID) -> None:
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
        if row is None or row[column.value] is None:
            return None
        try:
            value = self._cipher.decrypt(row[column.value])
        except SecretCryptoError as exc:
            raise McpOAuthStoreError(
                "stored MCP OAuth credential cannot be decrypted"
            ) from exc
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

    async def get_tokens(self) -> OAuthToken | None:
        async with self._gate.lock:
            self._active()
            return await self._store.get_tokens(self._server_id, self._user_id)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        async with self._gate.lock:
            self._active()
            await self._store.set_tokens(self._server_id, self._user_id, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        async with self._gate.lock:
            self._active()
            return await self._store.get_client_info(self._server_id, self._user_id)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        async with self._gate.lock:
            self._active()
            await self._store.set_client_info(
                self._server_id, self._user_id, client_info
            )

    def _active(self) -> None:
        if self._generation != self._gate.generation:
            raise McpOAuthStoreError("MCP OAuth credentials were disconnected")
