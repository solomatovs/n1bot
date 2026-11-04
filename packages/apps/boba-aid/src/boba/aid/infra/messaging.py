"""Совместный жизненный цикл транспорта, блокировок и тел сообщений.

Ошибки определяют реализации портов MessageBus, LiveLocks и PayloadStore.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from boba.db.postgres import PostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.locks import LiveLocks, StaleLock
from boba.messaging import MessageBus, MessageCodec, PayloadStore
from boba.messaging.config import AppName, ClusterConfig


class StaleObserver(Protocol):
    """Обработчик последствий потери держателя, предоставляемый приложением."""

    @abstractmethod
    async def close(self, stale: Sequence[StaleLock]) -> int: ...


StaleObserverFactory = Callable[[MessageBus, LiveLocks], StaleObserver]


@dataclass(frozen=True)
class MessagingRuntime:
    """Согласованный набор ресурсов одной реализации обмена сообщениями."""

    bus: MessageBus
    payloads: PayloadStore
    locks: LiveLocks


@dataclass(frozen=True)
class MessagingSettings:
    """Настройки обмена, которые сборка извлекает из конфигурации приложения."""

    postgres: PostgresConfig
    db_schema: str
    cluster: ClusterConfig


@dataclass(frozen=True)
class MessagingServices:
    """Ресурсы процесса для подключённого комплекта messaging; создаёт корень сборки."""

    config: MessagingSettings
    instance: str
    app: AppName
    pool: PostgresPool
    codec: MessageCodec

    observer: StaleObserverFactory
