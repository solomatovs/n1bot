"""Запечатывание соединений в вызовах стенда без агента.

В бою имя соединения в аргументах вызова заменяет ретранслятор чата на пути
агент → ретранслятор → сервер MCP. Стенд, который зовёт сервер инструментов напрямую,
делает тот же шаг этим портом: SealingStand читает схему инструмента
(SealedParamSchema), берёт соединение пользователя по виду и имени
(ArmedConnections), запечатывает его ключом сервера из его возможностей
(SealFeature) и передаёт вызов дальше. Создаётся тестом поверх порта
сервера; остальные аргументы вызова идут как есть.

Отказ в соединении (RefusalError) наружу не выходит: вызов кончается
итогом-отказом.

Ошибки:
ConnectionStoreError — таблица соединений недоступна.
KerberosError — билет к соединению не выпущен.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta

from pydantic import JsonValue, ValidationError

from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import (
    ConnectionSeal,
    SealedParamSchema,
    SealFeature,
    SealKey,
)
from boba.identity.errors import RefusalError
from boba.toolkit.dag import (
    DagNode,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolServer,
)
from boba.toolkit.failure import FailurePacker, ValidationText

__all__ = ["SealingStand"]


class SealingStand(ToolServer):
    """Реализация ToolServer стенда: запечатывает соединения вызова и отдаёт
    его порту сервера."""

    def __init__(
        self, inner: ToolServer, connections: ArmedConnections, ttl: timedelta
    ) -> None:
        self._inner = inner
        self._connections = connections
        self._ttl = ttl
        self._declared = SealedParamSchema()
        self._failures = FailurePacker()
        self._outcomes = NodeOutcomes()

    def tools(self) -> Sequence[ToolCard]:
        return self._inner.tools()

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return self._inner.features()

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        """Вызов, которому отказано в соединении, до сервера не доходит и
        кончается итогом-отказом — как отказ хука в бою."""
        pending: list[asyncio.Future[NodeOutcome]] = []
        for call in calls:
            try:
                sealed = await self.sealed(call)
            except RefusalError as exc:
                refused = self._outcomes.refused(call, self._failures.pack(exc))
                pending.append(self._outcomes.settled(refused))
                continue

            pending.extend(await self._inner.submit([sealed]))

        return pending

    async def sealed(self, call: DagNode) -> DagNode:
        """Вызов с запечатанными соединениями на месте имён; инструмент без
        параметров-соединений уходит как есть.

        Ошибки:
        RefusalError — сервер не принимает соединений либо имя не выдано
            вызывающему.
        """
        card = self._card_of(call.tool)
        if card is None:
            return call

        kinds = self._declared.kinds_of(card.parameters)
        if not kinds:
            return call

        seal = ConnectionSeal(self._key())
        sent: dict[str, JsonValue] = dict(call.args)
        for param, kind in kinds.items():
            name = call.args.get(param)
            if not isinstance(name, str):
                continue

            connection = await self._connections.sealed(kind, name, self._ttl)
            sent[param] = seal.seal(connection)

        return call.model_copy(update={"args": sent})

    def _card_of(self, tool: str) -> ToolCard | None:
        for card in self._inner.tools():
            if card.name == tool:
                return card

        return None

    def _key(self) -> SealKey:
        """Ключ сервера из объявленной им возможности."""
        declared = self._inner.features().get(SealFeature.ID)
        if declared is None:
            msg = (
                f"the tool server does not declare the feature {SealFeature.ID!r}, "
                "so it does not accept connections"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        try:
            return SealFeature.model_validate(declared).key
        except ValidationError as exc:
            msg = (
                f"the feature {SealFeature.ID!r} of the tool server does not carry "
                f"an encryption key: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg) from exc
