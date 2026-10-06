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
from datetime import timedelta
from typing import ClassVar

from boba.connection_broker.sealed import SealedConnectionParam
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
    SealKeys,
)
from boba.identity.context import CallContexts
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import AsyncInjected

__all__ = ["StandUserConnections"]


class StandConnectionParam(AsyncInjected):
    """Обвязка параметра-соединения стенда: путь клиента и исполнителя подряд.

    Значение параметра — ссылка ConnectionRef либо голое имя соединения вида
    параметра. Обвязка берёт соединение субъекта вызова (ArmedConnections),
    запечатывает его ключом стенда и отдаёт боевой обвязке исполнителя.
    Ставит её StandUserConnections.
    """

    def __init__(
        self,
        connections: ArmedConnections,
        executor: SealedConnectionParam,
        seal: ConnectionSeal,
        kind: str,
    ) -> None:
        super().__init__(executor.param, None)
        self._connections = connections
        self._executor = executor
        self._seal = seal
        self._kind = kind
        self._refs = ConnectionRefs()

    async def value(self, name: str, kwargs: dict[str, object]) -> object:
        requested = kwargs.get(self._param)
        if not isinstance(requested, str):
            return await self._executor.value(name, kwargs)

        sealed = await self._connections.sealed(
            self._ref(requested), StandUserConnections.TTL
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

    TTL: ClassVar[timedelta] = timedelta(minutes=10)

    def __init__(
        self,
        store_ref: StoreRef,
        credentials_ref: CredentialsRef,
        types_ref: TypesRef,
        contexts: CallContexts,
    ) -> None:
        self._types_ref = types_ref
        self._contexts = contexts
        self._keys = SealKeys()
        self._seal = ConnectionSeal(self._keys.public())
        self._connections = ArmedConnections(store_ref, credentials_ref, contexts)
        self._hooks = ConnectionParamHooks(types_ref, SealedConnectionParam.ARGUMENT)

    def bind_all(self, tools: Sequence[HostedTool]) -> None:
        self._hooks.bind_all(tools, self._hook)

    def _hook(self, tool: str, param: str, kind: str) -> AsyncInjected:
        executor = SealedConnectionParam(
            self._keys, self._types_ref, self._contexts, param, kind
        )

        return StandConnectionParam(self._connections, executor, self._seal, kind)
