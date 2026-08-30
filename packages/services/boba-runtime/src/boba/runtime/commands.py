"""Исполняет команды шины на стороне держателя: StopRequested останавливает запуск
или ход, который ведёт этот процесс.

Ошибки:
MessageBusError — шина не отдала команду на исполнение.
"""

from __future__ import annotations

import logging

from boba.cancellation import StopReason
from boba.identity.run import RunRegistry
from boba.messaging import CommandEnvelope, MessageBus, StopRequested, Unsubscribe

__all__ = ["CommandRunner"]

logger = logging.getLogger(__name__)


class CommandRunner:
    """Подписывается на команды шины и исполняет те, чья область ведётся этим
    процессом: забирает команду через take и останавливает область через
    RunRegistry.
    """

    def __init__(self, bus: MessageBus, instance: str) -> None:
        self._bus = bus
        self._instance = instance
        self._leave: Unsubscribe | None = None

    def start(self) -> None:
        if self._leave is None:
            self._leave = self._bus.subscribe_commands(self.handle)

    def stop(self) -> None:
        leave = self._leave
        self._leave = None
        if leave is not None:
            leave()

    async def handle(self, envelope: CommandEnvelope) -> None:
        scope_id = envelope.scope.id
        if RunRegistry.active(scope_id) is None:
            return

        taken = await self._bus.take(
            envelope.scope, envelope.command_id, self._instance
        )
        if not taken:
            logger.info("command %d already taken elsewhere", envelope.command_id)
            return

        command = envelope.command
        if isinstance(command, StopRequested):
            logger.info(
                "command %d: stop %s requested by user %s via %s",
                envelope.command_id,
                envelope.scope.render(),
                command.by_user,
                command.by_instance,
            )
            RunRegistry.stop(scope_id, StopReason.USER_STOP)
