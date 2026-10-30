"""Программа плагина инструментов: подкоманды manifest, setup, zygote и call.

`python -m boba.sandbox.program <id> <подкоманда> ...` находит объявление
плагина <id> (ToolPlugin) entry point'ом группы boba.tools в своём процессе;
в образе плагина её зовёт /plugin/run. Форма `--declared <модуль:имя>,...`
вместо id собирает плагин из объявлений по ссылкам — так стенды тестов
поднимают зиготу над инструментами нескольких модулей. Сервер код плагина
не импортирует: он запускает подкоманды и читает их вывод.

- manifest: stdin — JSON секции [tool.<id>]; программа проверяет секцию
  моделями плагина и печатает манифест JSON в stdout.
- setup: stdin — та же секция, профили соединений уже с билетами вызова;
  программа готовит хранилище плагина.
- zygote: аргументы ZygoteArgs; зигота плагина в песочнице.
- call <tool>: один вызов тела без зиготы (ToolMain).
- self-check: самопроверка бинарника Nuitka по описи сборки
  (PackageDataCheck): данные пакетов и entry points внутри; её зовёт сборка
  образа плагина. Из исходников описи нет, и подкоманда это говорит.

Ошибки:
PluginEntryError — PluginEntry.program не нашёл объявления плагина.
Подкоманды наружу не выпускают ничего: исход виден кодом возврата
ProgramExit и текстом в stderr.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from enum import IntEnum, StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import Any, ClassVar

from boba.core.package_data import PackageDataCheck, PackageDataError
from boba.sandbox.guest import ZygoteArgs, ZygoteMain
from boba.toolkit.entry import ToolMain
from boba.toolkit.manifest import (
    ManifestBuilder,
    ManifestError,
    PluginManifest,
    ToolPlugin,
)

__all__ = [
    "PluginCommand",
    "PluginEntry",
    "PluginEntryError",
    "PluginProgram",
    "ProgramExit",
]

logger = logging.getLogger(__name__)


class ProgramExit(IntEnum):
    """Коды возврата программы плагина."""

    OK = 0
    FAILURE = 1
    """Подкоманда сорвалась: хранилище не готово, объявление не по спецификации."""
    USAGE = 2
    """Командная строка не разобрана: нет id, подкоманды или имени инструмента."""
    CONFIG = 3
    """Секция плагина не подходит его моделям конфига."""


class PluginCommand(StrEnum):
    """Подкоманды программы плагина."""

    MANIFEST = "manifest"
    SETUP = "setup"
    ZYGOTE = "zygote"
    CALL = "call"
    SELF_CHECK = "self-check"


class PluginProgram:
    """Подкоманды программы над объявлением плагина.

    Создаёт его вход программы (PluginEntry) по найденному объявлению либо
    модуль тестовых инструментов со своим объявлением. Манифест и его
    отпечаток для зиготы строит ManifestBuilder.
    """

    def __init__(self, plugin: ToolPlugin, version: str) -> None:
        self._plugin = plugin
        self._version = version
        self._builder = ManifestBuilder()

    @property
    def plugin(self) -> ToolPlugin:
        return self._plugin

    def manifest(self) -> PluginManifest:
        """Манифест плагина, как его печатает подкоманда manifest."""
        return self._builder.manifest(self._plugin, self._version)

    def main(self, argv: Sequence[str]) -> int:
        """Исполнить подкоманду argv[0] с остальными аргументами."""
        if not argv:
            return self._usage("no subcommand given")

        name = argv[0]
        rest = list(argv[1:])

        if name == PluginCommand.CALL:
            return ToolMain.run(self._plugin.tools, rest)

        if name == PluginCommand.ZYGOTE:
            return self._zygote(rest)

        return self._without_args(name)

    def _without_args(self, name: str) -> int:
        """Подкоманды без аргументов: вход у них — stdin или опись сборки."""
        if name == PluginCommand.MANIFEST:
            return self._manifest()

        if name == PluginCommand.SETUP:
            return self._setup()

        if name == PluginCommand.SELF_CHECK:
            return self._self_check()

        return self._usage(f"unknown subcommand {name!r}")

    def _usage(self, reason: str) -> int:
        known = ", ".join(PluginCommand)
        self._fail(f"plugin {self._plugin.id}: {reason}; subcommands: {known}")

        return ProgramExit.USAGE

    def _zygote(self, rest: Sequence[str]) -> int:
        digest = self.manifest().digest()

        return ZygoteMain.run(ZygoteArgs.parse(rest), self._plugin, digest)

    def _manifest(self) -> int:
        """Секция со stdin проверяется моделями плагина, манифест — в stdout."""
        self._log_to_stderr()

        section = self._section()
        if section is None:
            return ProgramExit.CONFIG

        if not self._fits(section):
            return ProgramExit.CONFIG

        try:
            manifest = self._builder.manifest(self._plugin, self._version)
        except ManifestError as exc:
            self._fail(f"plugin {self._plugin.id}: {exc}")
            return ProgramExit.FAILURE

        sys.stdout.write(manifest.model_dump_json())
        sys.stdout.flush()

        return ProgramExit.OK

    def _setup(self) -> int:
        """Хранилище плагина по секции со stdin; повтор безвреден."""
        self._log_to_stderr()

        setup = self._plugin.setup
        if setup is None:
            self._fail(f"plugin {self._plugin.id} declares no setup")
            return ProgramExit.USAGE

        section = self._section()
        if section is None:
            return ProgramExit.CONFIG

        try:
            storage = setup.storage_of(self._plugin.id, section)
        except ManifestError as exc:
            self._fail(str(exc))
            return ProgramExit.CONFIG

        try:
            asyncio.run(storage.setup())
        except Exception as exc:
            self._fail(f"plugin {self._plugin.id}: preparing its storage failed: {exc}")
            return ProgramExit.FAILURE

        return ProgramExit.OK

    def _self_check(self) -> int:
        """Опись сборки внутри бинарника сходится с его содержимым."""
        try:
            checked = PackageDataCheck().run()
        except PackageDataError as exc:
            self._fail(f"plugin {self._plugin.id}: {exc}")
            return ProgramExit.FAILURE

        sys.stdout.write(f"plugin {self._plugin.id}: {checked}\n")
        sys.stdout.flush()

        return ProgramExit.OK

    def _fits(self, section: Mapping[str, Any]) -> bool:
        """Секция проходит каждую модель конфига плагина; иначе причины — в stderr."""
        problems = list(self._builder.unfit(self._plugin, section))
        for problem in problems:
            self._fail(problem)

        return not problems

    def _section(self) -> Mapping[str, Any] | None:
        """Секция [tool.<id>] со stdin; не JSON-объект — None и текст в stderr."""
        raw = sys.stdin.buffer.read()
        try:
            section = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            self._fail(
                f"plugin {self._plugin.id}: stdin expects the [tool.{self._plugin.id}] "
                f"section as JSON, got {len(raw)} bytes that do not parse: {exc}"
            )
            return None

        if not isinstance(section, dict):
            self._fail(
                f"plugin {self._plugin.id}: stdin expects the section as a JSON "
                f"object, got {type(section).__name__}"
            )
            return None

        return section

    @staticmethod
    def _log_to_stderr() -> None:
        """stdout подкоманды — её ответ: логи плагина уходят в stderr."""
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)

    @staticmethod
    def _fail(text: str) -> None:
        print(text, file=sys.stderr, flush=True)  # noqa: T201


class PluginEntryError(Exception):
    """Объявление плагина по id не найдено или не ToolPlugin."""


class PluginEntry:
    """Вход программы по id плагина: объявление ищется entry point'ом группы
    ToolPlugin.GROUP в окружении программы, версия — у его дистрибутива.
    program() отдаёт программу плагина; ею же пользуется отладочный запуск
    тела на хосте (boba.runtime.toolcli)."""

    MODULE: ClassVar[str] = "boba.sandbox.program"
    """Модуль входа: `python -m <MODULE> <id> ...` из окружения с плагином."""
    DECLARED: ClassVar[str] = "--declared"
    """Форма входа по ссылкам на объявления вместо id."""
    REF_SEPARATOR: ClassVar[str] = ","
    ATTR_SEPARATOR: ClassVar[str] = ":"
    UNKNOWN_VERSION: ClassVar[str] = "0"
    """Версия плагина без дистрибутива: объявлений по ссылкам."""

    def argv(self, python: str, plugin_id: str) -> tuple[str, ...]:
        """Команда программы плагина plugin_id интерпретатором python."""
        return (python, "-m", self.MODULE, plugin_id)

    def declared_argv(self, python: str, refs: Sequence[str]) -> tuple[str, ...]:
        """Команда программы над объявлениями refs (`модуль:имя`)."""
        return (python, "-m", self.MODULE, self.DECLARED, self.REF_SEPARATOR.join(refs))

    def declared(self, refs: Sequence[str]) -> PluginProgram:
        """Программа над объявлениями refs: первое — плагин, остальные
        добавляют свои инструменты и прогревы.

        Ошибки:
        PluginEntryError — ссылка не `модуль:имя`, модуль не импортируется
            или имя — не ToolPlugin.
        """
        plugins: list[ToolPlugin] = []
        for ref in refs:
            plugins.append(self._loaded(ref))

        if not plugins:
            msg = f"{self.DECLARED} expects at least one module:name reference"
            raise PluginEntryError(msg)

        joined = plugins[0]
        for plugin in plugins[1:]:
            joined = joined.joined(plugin)

        return PluginProgram(joined, self.UNKNOWN_VERSION)

    def _loaded(self, ref: str) -> ToolPlugin:
        module_name, separator, attr = ref.partition(self.ATTR_SEPARATOR)
        if not separator:
            msg = f"{self.DECLARED}: reference {ref!r} is not module:name"
            raise PluginEntryError(msg)

        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            msg = f"{self.DECLARED}: module {module_name!r} does not import: {exc}"
            raise PluginEntryError(msg) from exc

        plugin = getattr(module, attr, None)
        if not isinstance(plugin, ToolPlugin):
            msg = (
                f"{self.DECLARED}: {ref} is {type(plugin).__name__}, "
                "expected a ToolPlugin"
            )
            raise PluginEntryError(msg)

        return plugin

    def main(self, argv: Sequence[str]) -> int:
        if not argv:
            print(  # noqa: T201
                f"usage: python -m {self.MODULE} <id> <subcommand> ...",
                file=sys.stderr,
            )
            return ProgramExit.USAGE

        try:
            program, rest = self._program_of(argv)
        except PluginEntryError as exc:
            print(exc, file=sys.stderr)  # noqa: T201
            return ProgramExit.USAGE

        return program.main(rest)

    def _program_of(self, argv: Sequence[str]) -> tuple[PluginProgram, Sequence[str]]:
        if argv[0] != self.DECLARED:
            return self.program(argv[0]), argv[1:]

        if len(argv) < 2:  # noqa: PLR2004
            msg = f"{self.DECLARED} expects module:name references after it"
            raise PluginEntryError(msg)

        return self.declared(argv[1].split(self.REF_SEPARATOR)), argv[2:]

    def program(self, plugin_id: str) -> PluginProgram:
        """Программа плагина plugin_id.

        Ошибки:
        PluginEntryError — плагина нет среди entry points или entry point
            отдал не ToolPlugin.
        """
        entry = self._entry(plugin_id)
        plugin = entry.load()
        if not isinstance(plugin, ToolPlugin):
            msg = (
                f"entry point {entry.name!r} of group {ToolPlugin.GROUP} "
                f"({entry.value}): expected a ToolPlugin, got {type(plugin).__name__}"
            )
            raise PluginEntryError(msg)

        return PluginProgram(plugin, self._version(entry))

    def ref(self, plugin_id: str) -> str:
        """Ссылка `модуль:имя` на объявление плагина plugin_id.

        Ошибки:
        PluginEntryError — плагина нет среди entry points.
        """
        return self._entry(plugin_id).value

    @staticmethod
    def _entry(plugin_id: str) -> EntryPoint:
        found = tuple(entry_points(group=ToolPlugin.GROUP, name=plugin_id))
        if found:
            return found[0]

        installed: list[str] = []
        for entry in entry_points(group=ToolPlugin.GROUP):
            installed.append(entry.name)

        msg = (
            f"plugin {plugin_id!r} is not installed here: entry points of group "
            f"{ToolPlugin.GROUP} are {sorted(installed)}"
        )
        raise PluginEntryError(msg)

    def _version(self, entry: EntryPoint) -> str:
        dist = entry.dist
        if dist is None:
            return self.UNKNOWN_VERSION

        return dist.version


if __name__ == "__main__":
    sys.exit(PluginEntry().main(sys.argv[1:]))
