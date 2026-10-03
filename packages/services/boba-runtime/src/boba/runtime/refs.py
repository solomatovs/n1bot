"""Входы приложения: ссылки на сервисы процесса, резолвятся на каждый вызов."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from boba.connection_broker.user_connections import CredentialsRef, StoreRef
from boba.connections.manifest import ConnectionTypes
from boba.connections.sealed import SealKeys
from boba.identity.context import CallContexts
from boba.identity.locks import LiveLocks
from boba.messaging import MessageBus
from boba.messaging.bus import BusWatch
from boba.toolrun.registry import ToolRegistry

__all__ = ["RuntimeRefs"]


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
