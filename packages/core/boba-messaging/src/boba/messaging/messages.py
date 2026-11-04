"""Открытый контракт сообщений и доверенный каталог их моделей.

Ошибки:
MessageBusError — неизвестный вид, неверное тело или повтор регистрации.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class MessageBusError(Exception):
    """Шина не смогла сохранить, доставить или прочитать сообщение."""


class Message(BaseModel):
    """Базовый контракт события; поля и значение kind задаёт его доменный пакет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)


class Command(BaseModel):
    """Базовый контракт команды; исполнение определяет подписчик приложения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)


@dataclass(frozen=True)
class MessageSpec:
    """Регистрация события и права публикации; поступает из сборки, вне JSON."""

    model: type[Message]
    requires_lock: bool = False


ModelT = TypeVar("ModelT", bound=BaseModel)


class MessageCodec:
    """Разбирает сообщения моделями зарегистрированных типов для шины."""

    def __init__(
        self,
        events: Iterable[MessageSpec],
        commands: Iterable[type[Command]],
    ) -> None:
        self._events: dict[str, type[Message]] = {}
        self._commands: dict[str, type[Command]] = {}
        self._locked: set[str] = set()
        for spec in events:
            kind = self._register(self._events, spec.model)
            if spec.requires_lock:
                self._locked.add(kind)

        for model in commands:
            self._register(self._commands, model)

    def event(self, raw: object) -> Message:
        return self._parse(raw, self._events)

    def command(self, raw: object) -> Command:
        return self._parse(raw, self._commands)

    def checked_event(self, message: Message) -> Message:
        return self.event(message.model_dump(mode="json"))

    def checked_command(self, command: Command) -> Command:
        return self.command(command.model_dump(mode="json"))

    def requires_lock(self, message: Message) -> bool:
        if message.kind not in self._events:
            raise MessageBusError(f"message type {message.kind!r} is not registered")

        return message.kind in self._locked

    @staticmethod
    def _register(table: dict[str, type[ModelT]], model: type[ModelT]) -> str:
        field = model.model_fields.get("kind")
        if field is None:
            raise MessageBusError(f"message model {model.__name__} has no kind field")

        kind = field.default
        if not isinstance(kind, str):
            raise MessageBusError(
                f"message model {model.__name__}: kind needs a string default"
            )

        if not kind:
            raise MessageBusError(
                f"message model {model.__name__}: kind must not be empty"
            )

        if kind in table:
            raise MessageBusError(f"message kind {kind!r} is registered twice")

        table[kind] = model
        return kind

    @staticmethod
    def _parse(raw: object, table: Mapping[str, type[ModelT]]) -> ModelT:
        if not isinstance(raw, Mapping):
            raise MessageBusError(
                f"message body must be an object, got {type(raw).__name__}"
            )

        kind = raw.get("kind")
        if not isinstance(kind, str):
            raise MessageBusError("message body requires a string kind")

        model = table.get(kind)
        if model is None:
            raise MessageBusError(f"message kind {kind!r} is not registered")

        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise MessageBusError(
                f"reading message kind {kind!r} failed: {exc}"
            ) from exc
