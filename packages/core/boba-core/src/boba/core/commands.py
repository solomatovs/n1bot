"""Вызов команды пользователя текстом `/<имя> <аргументы>`.

Команду называет первое слово вопроса с косой чертой, остальное — её
аргументы. Слова аргументов раскладываются по объявленным аргументам
команды по порядку, последнему достаётся весь хвост; кавычки держат слово с
пробелами вместе. Так разбирают вызов и харнесс, и агент, которому вызов
приходит обычным вопросом.

Ошибки:
CommandArgumentsError — кавычки не закрыты или обязательного аргумента нет.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "CommandArgument",
    "CommandArgumentsError",
    "CommandCall",
    "CommandLines",
    "CommandMark",
]


class CommandArgumentsError(ValueError):
    """Аргументы команды не разложить по объявленным."""


class CommandMark(StrEnum):
    """Разметка вызова команды в тексте вопроса."""

    PREFIX = "/"
    SPACE = " "


@dataclass(frozen=True)
class CommandArgument:
    """Объявленный аргумент команды: имя и обязательность."""

    name: str
    required: bool


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
