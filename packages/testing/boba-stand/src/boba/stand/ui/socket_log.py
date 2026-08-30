"""Журнал socket.io: что именно сервер прислал вкладке и в каком порядке.

Потоковость шага доказывается здесь: у стримящегося шага сначала stream_start,
затем несколько stream_token, и только потом финальная отправка. Ошибки:
FrameError — фрейм не разобрать как событие socket.io.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["ChatEvent", "FrameError", "SocketFrame", "SocketLog"]


class FrameError(ValueError):
    """Фрейм socket.io не разобран."""


class ChatEvent(StrEnum):
    """События ленты, которые chainlit шлёт вкладке."""

    NEW_MESSAGE = "new_message"
    UPDATE_MESSAGE = "update_message"
    DELETE_MESSAGE = "delete_message"
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_token"
    TASK_START = "task_start"
    TASK_END = "task_end"
    ELEMENT = "element"


class StepField(StrEnum):
    """Поля StepDict, по которым тест узнаёт шаг."""

    ID = "id"
    TYPE = "type"
    NAME = "name"
    INPUT = "input"
    OUTPUT = "output"
    PARENT_ID = "parentId"
    THREAD_ID = "threadId"
    IS_ERROR = "isError"


@dataclass(frozen=True)
class SocketFrame:
    """Одно событие socket.io с полезной нагрузкой."""

    event: ChatEvent
    payload: Any

    @property
    def step_id(self) -> str:
        """Идентификатор шага: у токена он лежит в поле id полезной нагрузки."""
        if not isinstance(self.payload, dict):
            return ""

        value = self.payload.get(StepField.ID.value)
        if not value:
            return ""

        return str(value)

    @property
    def step_type(self) -> str:
        if not isinstance(self.payload, dict):
            return ""

        value = self.payload.get(StepField.TYPE.value)
        if not value:
            return ""

        return str(value)

    @property
    def token(self) -> str:
        if self.event is not ChatEvent.STREAM_CHUNK:
            return ""

        if not isinstance(self.payload, dict):
            return ""

        value = self.payload.get("token")
        if not value:
            return ""

        return str(value)


@dataclass
class SocketLog:
    """Собирает фреймы вкладки и отвечает на вопросы о потоковости."""

    frames: list[SocketFrame] = field(default_factory=list)

    def accept(self, raw: bytes | str) -> None:
        """Принимает фрейм; бинарные и служебные пакеты socket.io пропускаются."""
        if not isinstance(raw, str):
            return

        parsed = self._parse(raw)
        if parsed is None:
            return

        self.frames.append(parsed)

    def clear(self) -> None:
        self.frames.clear()

    def of_event(self, event: ChatEvent) -> list[SocketFrame]:
        found: list[SocketFrame] = []
        for frame in self.frames:
            if frame.event is event:
                found.append(frame)

        return found

    def tokens_of(self, step_id: str) -> list[str]:
        """Токены, пришедшие в конкретный шаг, в порядке прихода."""
        tokens: list[str] = []
        for frame in self.frames:
            if frame.event is not ChatEvent.STREAM_CHUNK:
                continue

            if frame.step_id != step_id:
                continue

            tokens.append(frame.token)

        return tokens

    def streamed_steps(self, step_type: str) -> list[str]:
        """Шаги указанного типа, у которых был stream_start."""
        found: list[str] = []
        for frame in self.of_event(ChatEvent.STREAM_START):
            if frame.step_type != step_type:
                continue

            found.append(frame.step_id)

        return found

    def index_of(self, event: ChatEvent, step_id: str) -> int:
        """Позиция события шага в общем потоке; -1 — события не было."""
        for index, frame in enumerate(self.frames):
            if frame.event is not event:
                continue

            if frame.step_id != step_id:
                continue

            return index

        return -1

    def steps_of_type(self, step_type: str) -> list[dict[str, Any]]:
        """Полезные нагрузки шагов указанного типа из new_message/update_message."""
        found: list[dict[str, Any]] = []
        for frame in self.frames:
            if frame.event not in (ChatEvent.NEW_MESSAGE, ChatEvent.UPDATE_MESSAGE):
                continue

            if frame.step_type != step_type:
                continue

            if not isinstance(frame.payload, dict):
                continue

            found.append(frame.payload)

        return found

    def last_step(self, step_type: str) -> dict[str, Any] | None:
        """Итоговая нагрузка последнего созданного шага указанного типа.

        Шаг приходит несколько раз (new_message, затем update_message):
        побеждает последняя отправка того шага, что появился позже всех.
        """
        latest: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for frame in self.frames:
            if frame.event not in (ChatEvent.NEW_MESSAGE, ChatEvent.UPDATE_MESSAGE):
                continue

            if frame.step_type != step_type:
                continue

            if not isinstance(frame.payload, dict):
                continue

            step_id = frame.step_id
            if step_id not in latest:
                order.append(step_id)

            latest[step_id] = frame.payload

        if not order:
            return None

        return latest[order[-1]]

    def thread_id(self) -> str:
        """Тред вкладки: его несёт каждый шаг, присланный сервером."""
        for frame in self.frames:
            if not isinstance(frame.payload, dict):
                continue

            value = frame.payload.get(StepField.THREAD_ID.value)
            if value:
                return str(value)

        raise FrameError("no step with a thread id in the socket log")

    def has_step_named(self, name: str) -> bool:
        """Был ли шаг с таким именем: так тест узнаёт служебный ход chainlit."""
        for frame in self.frames:
            if not isinstance(frame.payload, dict):
                continue

            if frame.payload.get(StepField.NAME.value) == name:
                return True

        return False

    def describe(self) -> str:
        """Короткая расшифровка потока — её печатает упавший тест."""
        lines: list[str] = []
        for frame in self.frames:
            lines.append(f"{frame.event.value} {frame.step_type} {frame.step_id[:8]}")

        return "\n".join(lines)

    @staticmethod
    def _parse(raw: str) -> SocketFrame | None:
        if not raw.startswith("42"):
            return None

        start = raw.find("[")
        if start < 0:
            return None

        try:
            body = json.loads(raw[start:])
        except json.JSONDecodeError as exc:
            raise FrameError(f"socket.io frame is not json: {raw[:60]!r}") from exc

        if not isinstance(body, list):
            return None

        if not body:
            return None

        name = str(body[0])
        if name not in set(ChatEvent):
            return None

        payload: Any = None
        if len(body) > 1:
            payload = body[1]

        return SocketFrame(event=ChatEvent(name), payload=payload)


def frames_of(log: SocketLog, events: Sequence[ChatEvent]) -> Iterator[SocketFrame]:
    """Фреймы указанных событий в порядке прихода."""
    wanted = set(events)
    for frame in log.frames:
        if frame.event in wanted:
            yield frame
