"""Параметры-соединения на стенде: клиент и исполнитель в одной обвязке.

В приложении соединение проходит два процесса: клиент по ссылке модели
берёт соединение пользователя и запечатывает его, исполнитель открывает и
отдаёт телу. Тест инструмента зовёт тело напрямую, без порта клиента,
поэтому здесь оба шага стоят одной обвязкой параметра: вызов с именем или
ссылкой проходит боевые ArmedConnections, ConnectionSeal и
SealedConnectionParam.

Ошибки:
RefusalError — соединение не выдано субъекту, выдано дважды, делегированных
    кредов нет либо исполнитель отверг запечатанное значение; kind из
    ConnectionRefusal.
ToolConfigError — параметр объявлен непригодной моделью соединения.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.connection_broker.sealed import SealedConnectionParam
from boba.connection_broker.sealing import SealingToolServer, SentConnections
from boba.connection_broker.tickets import CredentialsRef
from boba.connection_broker.user_connections import (
    ArmedConnections,
    ConnectionParamHooks,
    StoreRef,
    TypesRef,
)
from boba.connections.sealed import (
    ConnectionRef,
    ConnectionRefs,
    ConnectionSeal,
    SealedConnection,
    SealKeys,
)
from boba.identity.context import CallContext
from boba.runtime.refs import RuntimeRefs
from boba.toolkit.types import SecretReveal
from boba.toolrun.injected import AsyncInjected
from boba.toolrun.stream_calls import ToolServer

__all__ = ["StandSealedServer", "StandUserConnections"]


class StandConnectionParam(AsyncInjected):
    """Обвязка параметра-соединения стенда: путь клиента и исполнителя подряд.

    Значение параметра — ссылка ConnectionRef либо голое имя соединения вида
    параметра. Обвязка вооружает соединение субъекта вызова, запечатывает
    его ключом стенда и отдаёт боевой обвязке исполнителя. Ставит её
    StandUserConnections.
    """

    TTL: ClassVar[timedelta] = timedelta(minutes=10)

    def __init__(
        self,
        connections: ArmedConnections,
        executor: SealedConnectionParam,
        keys: SealKeys,
        kind: str,
    ) -> None:
        super().__init__(executor.param, None)
        self._connections = connections
        self._executor = executor
        self._seal = ConnectionSeal(keys.public())
        self._kind = kind
        self._refs = ConnectionRefs()

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        requested = kwargs.get(self._param)
        if not isinstance(requested, str):
            return await self._executor.value(name, kwargs)

        connection = await self._connections.armed(self._ref(requested))
        sealed = SealedConnection(
            login=CallContext.current().subject.login,
            expires_at=datetime.now(UTC) + self.TTL,
            profile=SecretReveal.dumped(connection),
        )

        sent = dict(kwargs)
        sent[self._param] = self._seal.seal(sealed)

        return await self._executor.value(name, sent)

    def _ref(self, requested: str) -> ConnectionRef:
        if self._refs.is_ref(requested):
            return self._refs.parse(requested)

        return ConnectionRef(kind=self._kind, name=requested)


class StandUserConnections:
    """Постановка StandConnectionParam на параметры-соединения инструментов.

    Создаётся тестом из хранилища соединений, источника кредов и реестра
    типов; ключевая пара стенда — своя на объект.
    """

    def __init__(
        self, store_ref: StoreRef, credentials_ref: CredentialsRef, types_ref: TypesRef
    ) -> None:
        self._types_ref = types_ref
        self._keys = SealKeys()
        self._connections = ArmedConnections(store_ref, credentials_ref)
        self._hooks = ConnectionParamHooks(types_ref, SealedConnectionParam.ARGUMENT)

    def bind_all(self, tools: Sequence[BaseTool]) -> None:
        self._hooks.bind_all(tools, self._hook)

    def _hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        executor = SealedConnectionParam(self._keys, self._types_ref, param, kind)

        return StandConnectionParam(self._connections, executor, self._keys, kind)


class StandSealedServer:
    """Порт клиента над исполнителем стенда: ссылки на соединения запечатываются.

    Тест потока чата собирает граф сам, без провайдеров приложения; здесь
    собирается та же пара, что в приложении: SealingToolServer поверх порта
    исполнителя, с соединениями и кредами стенда.
    """

    TTL: ClassVar[timedelta] = timedelta(minutes=10)

    def __init__(self, refs: RuntimeRefs) -> None:
        self._refs = refs
        self.sent = SentConnections()
        """Что порт отправил вместо ссылок: его же получает трасер ленты."""

    def over(self, executor: ToolServer) -> SealingToolServer:
        connections = ArmedConnections(
            self._refs.connection_store, self._refs.credentials
        )

        return SealingToolServer(executor, connections, self.sent, self.TTL)
