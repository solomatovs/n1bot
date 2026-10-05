"""Входы приложения: ссылки на сервисы процесса, резолвятся на каждый вызов."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from boba.connection_broker.user_connections import CredentialsRef, StoreRef
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.identity.locks import LiveLocks
from boba.identity.run import Runs
from boba.messaging import MessageBus
from boba.messaging.bus import BusWatch
from boba.toolkit.chain import CallAmbient
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.streams import CallJournals

__all__ = ["ExecRefs", "RuntimeRefs"]


@dataclass(frozen=True)
class ExecRefs:
    """Входы исполнения инструментов: то, из чего загрузчик (ToolLoader)
    собирает обвязки тел. Собирает провайдер процесса; одинаков у чата,
    studio и сервиса boba-mcp — шины, блокировок и хранилища соединений
    здесь нет."""

    connection_types: Callable[[], ConnectionTypes]
    credentials: CredentialsRef
    contexts: CallContexts
    journals: CallJournals
    ambient: CallAmbient
    seal_keys: SealKeys


@dataclass(frozen=True)
class RuntimeRefs:
    """Что приложение отдаёт api и обвязкам инструментов; собирает bootstrap."""

    tool_registry: Callable[[], Awaitable[ToolRegistry]]
    """Реестр инструментов процесса; собирается контейнером на первый запрос."""
    connection_store: StoreRef
    connection_types: Callable[[], ConnectionTypes]
    """Реестр установленных типов соединений; зовётся на запрос."""
    credentials: CredentialsRef
    """Источник кредов вызова: профиль соединения с билетом к его SPN."""
    contexts: CallContexts
    """Держатель контекста вызова процесса: его получают обвязки
    инструментов и исполнители."""
    runs: Runs
    """Реестр идущих запусков процесса: его получают те, кто открывает
    запуски и кто их останавливает."""
    journals: CallJournals
    """Журналы живого вывода вызовов процесса."""
    ambient: CallAmbient
    """Обстановка вызова инструмента процесса: приёмники журнала и ручка
    группы для исполнителей."""
    seal_keys: SealKeys
    """Ключевая пара исполнителя: ею открываются соединения, запечатанные
    клиентом; одна на процесс."""
    live_locks: Callable[[], LiveLocks]
    """Блокировки областей процесса; зовётся на вызов."""
    heartbeat_sec: float
    """Период подтверждения жизни блокировки держателем."""
    bus_watch: Callable[[], BusWatch]
    """Слушатель шины процесса: состояние для лампочки страницы."""
    message_bus: Callable[[], MessageBus]
    """Шина процесса: api публикует в неё изменения списков пользователя."""

    def execution(self) -> ExecRefs:
        """Часть входов, нужная загрузчику инструментов."""
        return ExecRefs(
            connection_types=self.connection_types,
            credentials=self.credentials,
            contexts=self.contexts,
            journals=self.journals,
            ambient=self.ambient,
            seal_keys=self.seal_keys,
        )
