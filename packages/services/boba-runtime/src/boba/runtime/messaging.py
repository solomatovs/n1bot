"""Совместный жизненный цикл транспорта, блокировок и тел сообщений.

Ошибки определяют реализации портов MessageBus, LiveLocks и PayloadStore.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from boba.db.postgres import PostgresPool
from boba.identity.locks import LiveLocks, StaleLock
from boba.messaging import MessageBus, MessageCodec, PayloadStore

if TYPE_CHECKING:
    from boba.runtime.config import AppName, RuntimeConfig


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
class MessagingServices:
    """Ресурсы процесса для подключённого комплекта messaging; создаёт корень сборки."""

    config: RuntimeConfig
    instance: str
    app: AppName
    pool: PostgresPool
    codec: MessageCodec

    observer: StaleObserverFactory
