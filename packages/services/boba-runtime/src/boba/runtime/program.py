"""Разбор команд приложения и служебный ABI собранной программы.

Ошибки: ValueError — повторные команды; SystemExit — разбор CLI.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar

__all__ = [
    "CommandSpec",
    "ConfigArguments",
    "ProgramCall",
    "ProgramExit",
    "ProgramTool",
]


class ProgramTool(StrEnum):
    """Служебные подкоманды собранной программы: идут без службы и её
    импортов. launcher и probe зовёт песочница (команды собирает ProgramRoots),
    self-check — сборка после компиляции и проверка образа."""

    LAUNCHER = "launcher"
    PROBE = "probe"
    SELF_CHECK = "self-check"
    IDENTITIES = "identities"


class ProgramExit(IntEnum):
    """Коды выхода служебных подкоманд."""

    OK = 0


@dataclass(frozen=True)
class CommandSpec:
    """Описание команды и установщик её аргументов, переданные приложением."""

    name: str
    help: str
    configure: Callable[[argparse.ArgumentParser], None] | None = None
    config_required: bool = True
    description: str = ""


@dataclass(frozen=True)
class ProgramCall:
    """Общий результат разбора; специфичные флаги читает обработчик команды."""

    command: str
    paths: tuple[Path, ...]
    arguments: argparse.Namespace


class ConfigArguments:
    """Добавляет переданные команды и одинаковые флаги путей конфигурации."""

    CONFIG: ClassVar[str] = "--config"

    def __init__(
        self, parser: argparse.ArgumentParser, commands: Sequence[CommandSpec]
    ) -> None:
        self._parser = parser
        self._commands = tuple(commands)
        names = [command.name for command in commands]
        if len(names) != len(set(names)):
            raise ValueError(f"CLI command names must be unique, got {names}")

    def install(self, *, required: bool = True) -> None:
        commands = self._parser.add_subparsers(dest="command", required=required)
        for command in self._commands:
            sub = commands.add_parser(
                command.name, help=command.help, description=command.description
            )
            if command.config_required:
                sub.add_argument(
                    self.CONFIG,
                    required=True,
                    action="append",
                    type=Path,
                    help="config.toml of this instance; repeat for overlays",
                )

            if command.configure is not None:
                command.configure(sub)

    def call(self, argv: Sequence[str] | None = None) -> ProgramCall:
        self.install()
        arguments = self._parser.parse_args(argv)
        paths: tuple[Path, ...] = ()
        if hasattr(arguments, "config"):
            paths = tuple(arguments.config)

        return ProgramCall(arguments.command, paths, arguments)
