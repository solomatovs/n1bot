"""Соединения инструментов на стороне исполнителя: приём запечатанных значений.

Исполнитель соединений не хранит. Клиент присылает профиль на месте
параметра-соединения запечатанным открытым ключом исполнителя
(boba.connections.sealed). Здесь две части этого пути:

- EncryptionKeyTool — инструмент encryption_key: отдаёт клиенту открытый ключ.
- SealedConnectionParams ставит на параметры-соединения обвязку
  SealedConnectionParam: она открывает значение, сверяет его с вызовом и
  отдаёт телу готовый профиль.

Ошибки:
RefusalError — значение не запечатано, запечатано другим ключом, повреждено,
    просрочено, выдано другому пользователю либо несёт соединение другого
    вида; kind из ConnectionRefusal.
ToolConfigError — параметр объявлен непригодной моделью соединения.
InjectedAsyncOnlyError — тело инструмента вызвано синхронно: профиль
    подставляется только в async-теле.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.connection_broker.user_connections import (
    CallerApplication,
    ConnectionParamHooks,
    TypesRef,
)
from boba.connections.base import ConnectionBase
from boba.connections.manifest import ConnectionTypesError
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import SealedConnection, SealKeys
from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.result import TableResult
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.injected import AsyncInjected

__all__ = ["EncryptionKeyTool", "SealedConnectionParam", "SealedConnectionParams"]

logger = logging.getLogger(__name__)


class EncryptionKeyTool:
    """Инструмент encryption_key: открытый ключ исполнителя для клиента.

    Клиент обязан запечатать соединение этим ключом, прежде чем отдать его
    инструменту. Ключ запрашивает модель обычным вызовом; клиент берёт его
    из строки результата (SealKey). Создаёт инструмент загрузчик из
    ключевой пары исполнителя.
    """

    NAME: ClassVar[str] = "encryption_key"

    def __init__(self, keys: SealKeys) -> None:
        self._keys = keys

    def build(self) -> BaseTool:
        return ToolBridge.as_structured_tool(self._tool())

    def _tool(self) -> PayloadTool:
        keys = self._keys

        @tool
        async def encryption_key() -> TableResult:
            """Получить ключ шифрования соединений этого сервера.
            Вызывать:
               - один раз перед первым вызовом инструмента с параметром-соединением
               - ещё раз, если инструмент ответил, что ключ устарел
            После вызова ссылки на соединения из connection_list передаются
            инструментам как есть."""
            return TableResult(rows=[keys.public().model_dump()])

        return encryption_key


class SealedConnectionParam(AsyncInjected):
    """Обвязка одного параметра-соединения на стороне исполнителя.

    Клиент прислал на месте параметра запечатанный профиль; обвязка
    открывает его ключом исполнителя, сверяет с вызовом (пользователь, срок,
    вид соединения), собирает профиль по реестру типов и подписывает его
    клиентом вызова. Тело получает готовый профиль. Ставит её
    SealedConnectionParams.
    """

    ARGUMENT: ClassVar[str] = (
        "Ссылка на соединение.\n"
        "   - значение колонки connection из connection_list или "
        "connection_search, как есть\n"
        "   - kind строки должен подходить инструменту, описание — задаче "
        "пользователя\n"
        f"   - перед первым вызовом с соединением вызови {EncryptionKeyTool.NAME}"
    )

    RESEAL_HINT: ClassVar[str] = (
        f"call {EncryptionKeyTool.NAME}, then repeat the call with the "
        "connection reference from connection_list"
    )

    def __init__(
        self, keys: SealKeys, types_ref: TypesRef, param: str, kind: str
    ) -> None:
        super().__init__(param, None)
        self._keys = keys
        self._types_ref = types_ref
        self._kind = kind
        self._caller = CallerApplication()

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        sealed = self._opened(name, kwargs.get(self._param))
        login = CallContext.current().subject.login

        if sealed.login != login:
            msg = (
                f"{name}: the connection in {self._param!r} is sealed for user "
                f"{sealed.login!r}, the call is made by {login!r}"
            )
            raise RefusalError(ConnectionRefusal.SEALED_FOR_ANOTHER_USER, msg)

        if sealed.expired(datetime.now(UTC)):
            msg = (
                f"{name}: the connection in {self._param!r} expired at "
                f"{sealed.expires_at.isoformat()}; {self.RESEAL_HINT}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_EXPIRED, msg)

        connection = self._profile(name, sealed)
        labelled = self._caller.labelled(connection, name)

        logger.info(
            "tool %s: sealed connection in %s (%s) %s",
            name,
            self._param,
            self._kind,
            labelled.trace(),
        )

        return labelled

    def _opened(self, tool_name: str, value: object) -> SealedConnection:
        if not isinstance(value, str):
            msg = (
                f"{tool_name} needs a sealed connection in {self._param!r}, got "
                f"{value!r}; {self.RESEAL_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NOT_SEALED, msg)

        try:
            return self._keys.open(value)
        except RefusalError as exc:
            msg = f"{tool_name}: {self._param!r}: {exc}; {self.RESEAL_HINT}"
            raise RefusalError(exc.kind, msg) from exc

    def _profile(self, tool_name: str, sealed: SealedConnection) -> ConnectionBase:
        try:
            connection = self._types_ref().parse(sealed.profile)
        except ConnectionTypesError as exc:
            msg = (
                f"{tool_name}: the sealed connection in {self._param!r} is not a "
                f"connection profile: {exc}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_DAMAGED, msg) from exc

        if connection.kind != self._kind:
            msg = (
                f"{tool_name}: {self._param!r} expects a connection of kind "
                f"{self._kind!r}, got a connection of kind {connection.kind!r}; "
                f"pick a connection of kind {self._kind!r} from connection_list"
            )
            raise RefusalError(ConnectionRefusal.ANOTHER_KIND, msg)

        return connection


class SealedConnectionParams:
    """Постановка SealedConnectionParam на параметры-соединения инструментов.

    Создаёт её сборка исполнителя из его ключевой пары и реестра типов
    соединений; сама постановка и правка схемы для LLM — общие с
    UserConnections (ConnectionParamHooks).
    """

    def __init__(self, keys: SealKeys, types_ref: TypesRef) -> None:
        self._keys = keys
        self._types_ref = types_ref
        self._hooks = ConnectionParamHooks(types_ref, SealedConnectionParam.ARGUMENT)

    def bind_all(self, tools: Sequence[BaseTool]) -> None:
        self._hooks.bind_all(tools, self._hook)

    def _hook(self, param: str, kind: str) -> AsyncInjected:
        return SealedConnectionParam(self._keys, self._types_ref, param, kind)
