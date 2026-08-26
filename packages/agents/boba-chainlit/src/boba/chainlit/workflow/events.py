"""Шина снимков запусков внутри процесса: раннер публикует, слушатели получают.

Слушатель — сокет страницы или тест; подписка живёт, пока её не сняли
возвращённой функцией. Запуск и его слушатели — в одном процессе.

Ошибки: своих не выпускает; ошибка слушателя журналируется и остальных не
трогает.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.workflow import RunState, RunStatus

__all__ = ["RunEvents", "RunListener", "RunSnapshot"]

logger = logging.getLogger(__name__)


class RunSnapshot(BaseModel):
    """Снимок запуска для слушателей: id, статус и состояние."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: RunStatus
    state: RunState

    @property
    def finished(self) -> bool:
        return self.status.terminal


RunListener = Callable[[RunSnapshot], Awaitable[None]]


class RunEvents:
    """Подписки на снимки по run_id."""

    def __init__(self) -> None:
        self._listeners: dict[UUID, list[RunListener]] = {}

    def listen(self, run_id: UUID, listener: RunListener) -> Callable[[], None]:
        """Подписывает слушателя; итог — как его снять."""
        self._listeners.setdefault(run_id, []).append(listener)

        def leave() -> None:
            listeners = self._listeners.get(run_id)
            if listeners is None:
                return

            if listener in listeners:
                listeners.remove(listener)

            if not listeners:
                del self._listeners[run_id]

        return leave

    def listeners_of(self, run_id: UUID) -> int:
        return len(self._listeners.get(run_id, ()))

    async def publish(self, snapshot: RunSnapshot) -> None:
        listeners = list(self._listeners.get(snapshot.run_id, ()))
        for listener in listeners:
            try:
                await listener(snapshot)
            except Exception:
                logger.exception("run %s: listener failed", snapshot.run_id)
