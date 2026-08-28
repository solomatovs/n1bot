"""Шина в памяти одного процесса для тестов и стенда без базы; контракт тот же, что у
Postgres-реализации, доставка — внутри publish.

Ошибки:
MessageTooLargeError — тело сообщения больше лимита.
ListenerFailedError — подписчик не справился с конвертом.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from boba.identity.context import Scope
from boba.messaging.bus import (
    CommandEnvelope,
    CommandListener,
    Envelope,
    Listener,
    ListenerFailedError,
    LockToken,
    MessageBus,
    MessageTooLargeError,
    Unsubscribe,
)
from boba.messaging.messages import AnyCommand, AnyMessage

__all__ = ["MemoryMessageBus"]


class MemoryMessageBus(MessageBus):
    """Хранит сообщения и команды в словарях по области и доставляет их подписчикам
    прямо при публикации.
    """

    def __init__(self, origin: str) -> None:
        self._origin = origin
        self._events: dict[Scope, list[Envelope]] = {}
        self._commands: dict[Scope, list[CommandEnvelope]] = {}
        self._taken: dict[int, str] = {}
        self._listeners: dict[Scope, list[Listener]] = {}
        self._command_listeners: list[CommandListener] = []
        self._next_command = 1

    async def publish(self, scope: Scope, message: AnyMessage, token: LockToken) -> int:
        stored = self._events.setdefault(scope, [])

        seq = 1
        if stored:
            seq = stored[-1].seq + 1

        try:
            envelope = Envelope(
                scope=scope,
                seq=seq,
                at=datetime.now(UTC),
                origin=self._origin,
                message=message,
            )
        except ValidationError as exc:
            raise MessageTooLargeError(str(exc)) from exc

        stored.append(envelope)

        failures: list[BaseException] = []
        for listener in list(self._listeners.get(scope, ())):
            try:
                await listener(envelope)
            except Exception as exc:
                failures.append(exc)

        if failures:
            raise ListenerFailedError(
                f"{scope}: listener failed on seq {seq}", failures
            )

        return seq

    async def command(self, scope: Scope, command: AnyCommand) -> int:
        command_id = self._next_command
        self._next_command += 1

        envelope = CommandEnvelope(
            scope=scope, command_id=command_id, at=datetime.now(UTC), command=command
        )
        self._commands.setdefault(scope, []).append(envelope)

        failures: list[BaseException] = []
        for listener in list(self._command_listeners):
            try:
                await listener(envelope)
            except Exception as exc:
                failures.append(exc)

        if failures:
            what = f"{scope.render()}: command listener failed on {command_id}"
            raise ListenerFailedError(what, failures)

        return command_id

    def subscribe(self, scope: Scope, listener: Listener) -> Unsubscribe:
        listeners = self._listeners.setdefault(scope, [])
        listeners.append(listener)

        def leave() -> None:
            current = self._listeners.get(scope)
            if current is None:
                return

            if listener in current:
                current.remove(listener)

            if not current:
                del self._listeners[scope]

        return leave

    def subscribe_commands(self, listener: CommandListener) -> Unsubscribe:
        self._command_listeners.append(listener)

        def leave() -> None:
            if listener in self._command_listeners:
                self._command_listeners.remove(listener)

        return leave

    async def replay(self, scope: Scope, after_seq: int) -> Sequence[Envelope]:
        stored = self._events.get(scope, ())
        return [envelope for envelope in stored if envelope.seq > after_seq]

    async def take(self, scope: Scope, command_id: int, instance: str) -> bool:
        if command_id in self._taken:
            return False

        self._taken[command_id] = instance
        return True

    async def purge(self, scope: Scope) -> int:
        removed = len(self._events.pop(scope, ()))

        for envelope in self._commands.pop(scope, ()):
            self._taken.pop(envelope.command_id, None)

        return removed

    def listeners_of(self, scope: Scope) -> int:
        return len(self._listeners.get(scope, ()))
