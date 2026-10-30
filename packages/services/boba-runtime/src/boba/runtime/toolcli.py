"""Запуск тела инструмента на хосте по toml приложения.

`python -m boba.runtime.toolcli <id> <имя> --config <toml> [--config <toml>]
[--args <json>]` находит плагин <id> его entry point'ом, поднимает рабочий
каталог kerberos из [krb], добавляет к аргументам модели из файла --args
секцию [tool.<id>] на места параметров роли config и in-process исполняет
подкоманду call программы плагина — тело идёт под отладчиком без песочницы.

Ошибки:
ToolCliError — argv не разобран: нет id, имени или пути --config;
    плагин не найден, инструмента в нём нет, файл --args не JSON-объект,
    тело ждёт значение контекста вызова, которого у CLI нет.
Код возврата тела — ToolMain.Exit, как у подкоманды call.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from omegaconf import DictConfig

from boba.config import ConfigLoader, bind
from boba.krb import KerberosWorkspaceConfig
from boba.runtime.plugins import PluginSections
from boba.sandbox.program import PluginCommand, PluginEntry, PluginEntryError
from boba.toolkit.entry import EntryFlag, ToolMain
from boba.toolkit.manifest import InjectedRole, ManifestBuilder, ToolDecl, ToolPlugin

__all__ = ["HostConfig", "ToolCli", "ToolCliError"]


class ToolCliError(Exception):
    """Команда CLI не разобрана или модуль инструментов не пригоден."""


class CliFlag(StrEnum):
    """Флаги CLI поверх аргументов подкоманды call."""

    CONFIG = "--config"


class HostConfig:
    """Toml приложения при запуске тела на хосте: каталог kerberos и секции плагинов."""

    KERBEROS_SECTION: ClassVar[str] = "krb"

    def __init__(self, paths: Sequence[Path]) -> None:
        self._raw: DictConfig = ConfigLoader(paths).load()
        self._sections = PluginSections(self._raw)

    def enter_kerberos(self) -> None:
        """Рабочий каталог kerberos из [krb]; без секции keytab-профили телу закрыты."""
        if self.KERBEROS_SECTION not in self._raw:
            return

        bind(self._raw, self.KERBEROS_SECTION, KerberosWorkspaceConfig).apply()

    def section(self, plugin_id: str) -> dict[str, Any]:
        """Секция [tool.<id>] значениями JSON; нет секции — отказ."""
        try:
            return self._sections.values(plugin_id)
        except RuntimeError as exc:
            msg = f"toolcli: {exc}"
            raise ToolCliError(msg) from exc


class ToolCli:
    """argv CLI -> плагин, имя, toml -> подкоманда call программы плагина in-process."""

    ARGS_FILE: ClassVar[str] = "args.json"
    CONTEXT: ClassVar[frozenset[InjectedRole]] = frozenset(
        {InjectedRole.SUBJECT, InjectedRole.SCOPE, InjectedRole.WORKSPACE_ROOT}
    )
    """Роли контекста вызова: у CLI нет пользователя, значения даёт файл --args."""
    USAGE: ClassVar[str] = (
        "usage: python -m boba.runtime.toolcli <plugin id> <tool> "
        f"{CliFlag.CONFIG} <toml> [{CliFlag.CONFIG} <overlay toml>] "
        f"[{EntryFlag.ARGS} <json>]"
    )

    def __init__(self) -> None:
        self._entry = PluginEntry()
        self._builder = ManifestBuilder()

    def main(self, argv: Sequence[str]) -> int:
        try:
            return self._run(list(argv))
        except ToolCliError as exc:
            print(f"{exc}\n{self.USAGE}", file=sys.stderr)  # noqa: T201
            return ToolMain.Exit.ENTRY_ERROR

    def _run(self, arguments: list[str]) -> int:
        if len(arguments) < 2:  # noqa: PLR2004
            msg = (
                "toolcli: expected a plugin id and a tool name as the first "
                f"two arguments, got {arguments!r}"
            )
            raise ToolCliError(msg)

        plugin_id = arguments.pop(0)
        tool_name = arguments.pop(0)

        paths = self._pop_paths(arguments, CliFlag.CONFIG)
        args = self._args(arguments)

        try:
            program = self._entry.program(plugin_id)
        except PluginEntryError as exc:
            msg = f"toolcli: {exc}"
            raise ToolCliError(msg) from exc

        host = HostConfig(paths)
        host.enter_kerberos()

        decl = self._decl(program.plugin, tool_name)
        self._served(decl, args, host.section(plugin_id))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.ARGS_FILE
            path.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")

            call = [
                PluginCommand.CALL.value,
                tool_name,
                EntryFlag.ARGS.value,
                str(path),
            ]
            return program.main([*call, *arguments])

    def _served(
        self, decl: ToolDecl, args: dict[str, Any], section: dict[str, Any]
    ) -> None:
        """Секция плагина на места параметров роли config; значения контекста
        вызова из toml не собрать — их обязан дать файл --args."""
        for param, role in decl.injected.items():
            if role is InjectedRole.CONFIG:
                args.setdefault(param, section)
                continue

            if role not in self.CONTEXT:
                continue

            if param in args:
                continue

            msg = (
                f"toolcli: parameter {param!r} of {decl.name!r} is the {role} of "
                f"the call and cannot be built from the toml; give it in the "
                f"{EntryFlag.ARGS} file"
            )
            raise ToolCliError(msg)

    def _decl(self, plugin: ToolPlugin, name: str) -> ToolDecl:
        known: list[str] = []
        for tool in plugin.tools:
            if tool.name == name:
                return self._builder.tool(tool)

            known.append(tool.name)

        msg = (
            f"toolcli: unknown tool {name!r}; plugin {plugin.id!r} declares: "
            f"{sorted(known)}"
        )
        raise ToolCliError(msg)

    def _args(self, arguments: list[str]) -> dict[str, Any]:
        """Аргументы модели из файла --args; без флага — пусто."""
        if EntryFlag.ARGS not in arguments:
            return {}

        path = self._pop_path(arguments, EntryFlag.ARGS)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            msg = f"toolcli: {path} is not JSON: {exc}"
            raise ToolCliError(msg) from exc

        if not isinstance(loaded, dict):
            msg = (
                f"toolcli: {path} must hold a JSON object, got {type(loaded).__name__}"
            )
            raise ToolCliError(msg)

        args: dict[str, Any] = {}
        for key, value in loaded.items():
            args[str(key)] = value

        return args

    def _pop_paths(self, arguments: list[str], flag: str) -> Sequence[Path]:
        """Все пути повторяемого флага: первый — конфиг, следующие — наложения."""
        paths = [self._pop_path(arguments, flag)]
        while flag in arguments:
            paths.append(self._pop_path(arguments, flag))

        return paths

    @staticmethod
    def _pop_path(arguments: list[str], flag: str) -> Path:
        if flag not in arguments:
            msg = (
                f"toolcli: {flag} <file> is required but missing "
                f"from the arguments {arguments!r}"
            )
            raise ToolCliError(msg)

        index = arguments.index(flag)
        if index + 1 >= len(arguments):
            msg = f"toolcli: {flag} is the last argument, expected a path after it"
            raise ToolCliError(msg)

        arguments.pop(index)
        raw = arguments.pop(index)

        path = Path(raw)
        if not path.is_file():
            msg = f"toolcli: {flag} expects an existing file, got {path}"
            raise ToolCliError(msg)

        return path


if __name__ == "__main__":
    sys.exit(ToolCli().main(sys.argv[1:]))
