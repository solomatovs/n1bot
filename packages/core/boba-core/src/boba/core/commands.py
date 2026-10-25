"""Вызов команды пользователя текстом `/<имя> <аргументы>`.

Команду называет первое слово вопроса с косой чертой, остальное — её
аргументы. Слова аргументов раскладываются по объявленным аргументам
команды по порядку, последнему достаётся весь хвост; кавычки держат слово с
пробелами вместе. Так разбирают вызов и харнесс, и агент, которому вызов
приходит обычным вопросом.

Здесь же пометка промпта сервера MCP расширением команд com.boba/commands
(PromptCommand): её ставит сервер, разбирает агент, объявляя команды сессии.

Ошибки:
CommandArgumentsError — кавычки не закрыты или обязательного аргумента нет.
ValidationError — пометка промпта не по модели PromptCommand.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CommandAction",
    "CommandArgument",
    "CommandArgumentsError",
    "CommandCall",
    "CommandLines",
    "CommandMark",
    "PromptCommand",
    "PromptMarks",
]


class CommandArgumentsError(ValueError):
    """Аргументы команды не разложить по объявленным."""


class CommandMark(StrEnum):
    """Разметка вызова команды в тексте вопроса."""

    PREFIX = "/"
    SPACE = " "


@dataclass(frozen=True)
class CommandArgument:
    """Объявленный аргумент команды: имя, обязательность и значения, которые
    подсказал владелец команды."""

    name: str
    required: bool
    values: Sequence[str] = ()


@dataclass(frozen=True)
class CommandCall:
    """Вызов команды name со строкой аргументов rest, как её набрал
    пользователь."""

    name: str
    rest: str

    def text(self) -> str:
        """Вызов текстом вопроса: `/<имя>` и аргументы через пробел."""
        head = f"{CommandMark.PREFIX.value}{self.name}"
        if not self.rest:
            return head

        return f"{head}{CommandMark.SPACE.value}{self.rest}"


class CommandLines:
    """Разбор вызова команды из текста вопроса и раскладка его аргументов.

    Держат полем реестр команд вкладки харнесса и серверы MCP сессии агента:
    у обоих вызов приходит текстом `/<имя> <аргументы>`.
    """

    def parsed(self, text: str) -> CommandCall | None:
        """Вызов команды в тексте; None — текст не начинается с `/<имя>`."""
        stripped = text.strip()
        if not stripped.startswith(CommandMark.PREFIX):
            return None

        head, _, rest = stripped.partition(CommandMark.SPACE)
        name = head.removeprefix(CommandMark.PREFIX)
        if not name:
            return None

        return CommandCall(name=name, rest=rest.strip())

    def hint(self, declared: Sequence[CommandArgument]) -> str:
        """Подсказка ввода аргументов: имена по порядку, у аргумента с
        подсказанными значениями — они через «|» в скобках."""
        parts: list[str] = []
        for argument in declared:
            if not argument.values:
                parts.append(argument.name)
                continue

            listed = "|".join(argument.values)
            parts.append(f"{argument.name} ({listed})")

        return CommandMark.SPACE.join(parts)

    def arguments(
        self, call: CommandCall, declared: Sequence[CommandArgument]
    ) -> dict[str, str]:
        """Слова аргументов вызова — объявленным аргументам по порядку;
        лишние слова — последнему аргументу.

        Ошибки:
        CommandArgumentsError — кавычки не закрыты или обязательного
            аргумента нет.
        """
        try:
            words = shlex.split(call.rest)
        except ValueError as exc:
            msg = (
                f"command /{call.name}: the arguments {call.rest!r} do not parse: {exc}"
            )
            raise CommandArgumentsError(msg) from exc

        arguments: dict[str, str] = {}
        for position, argument in enumerate(declared):
            if position >= len(words):
                break

            last = position == len(declared) - 1
            if last:
                arguments[argument.name] = CommandMark.SPACE.join(words[position:])
                break

            arguments[argument.name] = words[position]

        for argument in declared:
            if not argument.required:
                continue

            if argument.name in arguments:
                continue

            order: list[str] = []
            for item in declared:
                order.append(item.name)

            msg = (
                f"command /{call.name}: argument {argument.name!r} is required; "
                f"give the arguments in order: {order}"
            )
            raise CommandArgumentsError(msg)

        return arguments


class CommandAction(StrEnum):
    """Что делает команда, помеченная расширением команд."""

    APP = "app"
    """Открывает приложение MCP Apps инструмента, модель не участвует."""


class PromptCommand(BaseModel):
    """Пометка промпта расширением команд com.boba/commands: команда action
    ведёт к инструменту tool того же сервера, у которого есть приложение MCP
    Apps (_meta.ui.resourceUri) и "app" в visibility. Аргументы промпта —
    аргументы этого вызова. Сервер кладёт пометку в _meta промпта (meta),
    агент разбирает её из prompts/list (PromptMarks)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ID: ClassVar[str] = "com.boba/commands"
    """Ключ расширения: в возможностях сторон и в _meta промпта."""

    action: CommandAction
    tool: str = Field(min_length=1)

    def meta(self) -> dict[str, Any]:
        """_meta промпта с этой пометкой."""
        return {self.ID: self.model_dump(mode="json")}


class PromptMarks:
    """Разбор пометки расширения команд из _meta промпта сервера MCP. Держит
    полем вход агента, который объявляет команды серверов сессии."""

    def of(self, meta: Mapping[str, Any] | None) -> PromptCommand | None:
        """Пометка из _meta промпта; None — промпт обычный.

        Ошибки:
        ValidationError — пометка не по модели PromptCommand.
        """
        if meta is None:
            return None

        marked = meta.get(PromptCommand.ID)
        if marked is None:
            return None

        return PromptCommand.model_validate(marked)
