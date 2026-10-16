"""Входы приложения: ссылки на сервисы процесса, резолвятся на каждый вызов."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from boba.connection_broker.sealed import SealedConnectionParams
from boba.connection_broker.user_connections import CredentialsRef, StoreRef
from boba.connections.manifest import ConnectionTypes
from boba.identity.context import CallContexts
from boba.toolkit.chain import CallAmbient
from boba.toolrun.streams import CallJournals

__all__ = ["ExecRefs", "RuntimeRefs"]


@dataclass(frozen=True)
class ExecRefs:
    """Входы исполнения инструментов: то, из чего загрузчик (ToolLoader)
    собирает обвязки тел. Собирает провайдер процесса; одинаков у чата и
    сервиса boba-mcp — шины, блокировок и хранилища соединений здесь нет."""

    connection_types: Callable[[], ConnectionTypes]
    credentials: CredentialsRef
    contexts: CallContexts
    journals: CallJournals
    ambient: CallAmbient
    connection_params: SealedConnectionParams


@dataclass(frozen=True)
class RuntimeRefs:
    """Входы обвязок инструментов: хранилище соединений и часть исполнения
    (execution); собирает стенд StandRefs."""

    connection_store: StoreRef
    connection_types: Callable[[], ConnectionTypes]
    """Реестр установленных типов соединений; зовётся на запрос."""
    credentials: CredentialsRef
    """Источник кредов вызова: профиль соединения с билетом к его SPN."""
    contexts: CallContexts
    """Держатель контекста вызова процесса: его получают обвязки
    инструментов и исполнители."""
    journals: CallJournals
    """Журналы живого вывода вызовов процесса."""
    ambient: CallAmbient
    """Обстановка вызова инструмента процесса: приёмники журнала и ручка
    группы для исполнителей."""
    connection_params: SealedConnectionParams
    """Соединения инструментов исполнителя: ключевая пара, которой
    открываются соединения, запечатанные клиентом, и заданные соединения
    сервера; одни на процесс."""

    def execution(self) -> ExecRefs:
        """Часть входов, нужная загрузчику инструментов."""
        return ExecRefs(
            connection_types=self.connection_types,
            credentials=self.credentials,
            contexts=self.contexts,
            journals=self.journals,
            ambient=self.ambient,
            connection_params=self.connection_params,
        )
