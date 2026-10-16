"""Запечатывание соединений в аргументах вызова инструмента сервера MCP на
стороне клиента.

Какие аргументы — соединения, какого вида и что сервер предлагает на их
месте, клиент читает из схемы инструмента (SealedParamSchema.offers): имя
соединения пользователя заменяется профилем, запечатанным ключом сервера из
его возможностей (SealFeature), имя заданного соединения сервера уходит как
есть. Соединение пользователя по виду и имени берёт ArmedConnections в
контексте вызова, который ставит вызывающий.

Ошибки:
RefusalError — соединение неизвестно или не выдано пользователю либо сервер
    инструмента не принимает соединений; kind из ConnectionRefusal.
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
    сервера, имя заданного соединения сервера уходит как есть, иначе вызов
    получает отказ.

    Создаётся провайдером ретранслятора чата (call_sealing) и стендом без
    агента (SealingStand); зовут его ретранслятор mcp/message и стенд в
    контексте вызывающего. Что предложено на месте параметра, читает из
    схемы инструмента (SealedParamSchema.offers): параметр без ветки
    запечатанного значения не трогается; имя, которое есть и у
    пользователя, и среди заданных соединений сервера, — соединение
    пользователя, с предупреждением в лог.
    """

    def __init__(self, connections: ArmedConnections, ttl: timedelta) -> None:
        self._connections = connections
        self._ttl = ttl
        self._declared = SealedParamSchema()

    async def seal(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        tool_schema: Mapping[str, Any],
        server: McpServerInfo,
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
        offers = self._declared.offers(tool_schema)
        if not offers:
            return updated

        for param, offer in offers.items():
            name = updated.get(param)
            if not isinstance(name, str):
                continue

            if not offer.sealed:
                continue

            seal = ConnectionSeal(self._key(server))
            kind = offer.kind
            try:
                sealed = await self._connections.sealed(kind, name, self._ttl)
            except RefusalError as exc:
                if exc.kind != ConnectionRefusal.NOT_VISIBLE:
                    raise

                if name not in offer.names:
                    raise

                continue

            if name in offer.names:
                logger.warning(
                    "sealing: connection %s of kind %s is both yours and a server "
                    "connection of %s, yours is used",
                    name,
                    kind,
                    tool,
                )

            updated[param] = seal.seal(sealed)

            logger.info(
                "sealing: connection %s of kind %s sealed in %s of %s",
                name,
                kind,
                param,
                tool,
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
