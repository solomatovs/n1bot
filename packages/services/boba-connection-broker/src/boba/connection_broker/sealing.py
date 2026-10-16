"""Запечатывание соединений пользователя в аргументах вызова инструмента
сервера MCP: имя соединения заменяется профилем, запечатанным ключом
сервера из объявленных им возможностей (SealFeature); неизвестное или
невыданное соединение — отказ с причиной.

Ошибки:
RefusalError — соединение неизвестно, не выдано пользователю или сервер
    инструмента не принимает соединений.
ToolConfigError — соединение не годится инструменту.
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedParamSchema,
    SealFeature,
    SealKey,
)
from boba.identity.context import CallContext, CallContexts
from boba.identity.errors import RefusalError
from boba.toolkit.failure import ValidationText

__all__ = ["CallSealing", "McpServerInfo"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpServerInfo:
    """Сервер MCP вызываемого инструмента и возможности, которые он объявил:
    по ним запечатывание находит ключ сервера."""

    name: str
    features: Mapping[str, Mapping[str, object]]


class CallSealing:
    """Запечатывание соединений в аргументах вызова инструмента сервера MCP:
    имя соединения пользователя заменяется профилем, запечатанным ключом
    сервера, либо вызов получает отказ.

    Создаётся провайдером call_sealing чата; зовёт ретранслятор mcp/message.
    Какие аргументы — соединения и какого вида, читает из схемы
    инструмента (SealedParamSchema); ключ сервера — из объявленных им
    возможностей (SealFeature); соединение пользователя по виду и имени
    берёт ArmedConnections в контексте вызывающего.
    """

    def __init__(
        self, connections: ArmedConnections, contexts: CallContexts, ttl: timedelta
    ) -> None:
        self._connections = connections
        self._contexts = contexts
        self._ttl = ttl
        self._declared = SealedParamSchema()

    async def seal(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        tool_schema: Mapping[str, Any],
        server: McpServerInfo,
        context: CallContext,
    ) -> dict[str, Any]:
        """Аргументы вызова tool с запечатанными соединениями на месте имён;
        у инструмента без параметров-соединений — как есть. Значение не
        строка уходит как есть, его отвергнет сервер.

        Ошибки:
        RefusalError — соединение неизвестно или не выдано пользователю либо
            сервер инструмента не принимает соединений.
        ToolConfigError — соединение не годится инструменту.
        ConnectionStoreError — таблица соединений недоступна.
        KerberosError — билет к соединению не выпущен.
        """
        updated: dict[str, Any] = dict(arguments)
        kinds = self._declared.kinds_of(tool_schema)
        if not kinds:
            return updated

        seal = ConnectionSeal(self._key(server))
        with self._contexts.applied(context):
            for param, kind in kinds.items():
                name = updated.get(param)
                if not isinstance(name, str):
                    continue

                sealed = await self._connections.sealed(kind, name, self._ttl)
                updated[param] = seal.seal(sealed)

                logger.info(
                    "sealing: connection %s of kind %s sealed in %s of %s for %s",
                    name,
                    kind,
                    param,
                    tool,
                    context.subject.login,
                )

        return updated

    def _key(self, server: McpServerInfo) -> SealKey:
        """Ключ сервера инструмента из возможностей, которые он объявил.

        Ошибки:
        RefusalError — сервер не принимает соединений.
        """
        declared = server.features.get(SealFeature.ID)
        if declared is None:
            msg = (
                f"mcp server {server.name!r} does not declare the feature "
                f"{SealFeature.ID!r}, so it does not accept connections"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        try:
            return SealFeature.model_validate(declared).key
        except ValidationError as exc:
            msg = (
                f"mcp server {server.name!r}: the feature {SealFeature.ID!r} does "
                f"not carry an encryption key: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg) from exc
