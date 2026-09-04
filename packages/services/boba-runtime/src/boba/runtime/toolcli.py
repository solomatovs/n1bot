"""Запуск тела инструмента на хосте по toml приложения.

`python -m boba.runtime.toolcli <модуль> <имя> --флаги --config <toml>
[--injected <json>]` импортирует модуль инструментов, поднимает рабочий каталог
kerberos из [krb],
собирает injected-конфиг из секций toml (если не дан файлом --injected) и
in-process зовёт ToolMain.run модуля — тело исполняется под отладчиком без
песочницы.

Ошибки:
ToolCliError — argv не разобран: нет модуля, имени или пути --config, модуль
    не импортируется или не объявляет TOOLS.
Код возврата тела — ToolMain.Exit, как у команды модуля.
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from omegaconf import DictConfig

from boba.config import bind
from boba.krb import KerberosWorkspaceConfig
from boba.runtime.config import AppLayers
from boba.toolkit.entry import EntryFlag, ToolArgv, ToolLike, ToolMain

__all__ = ["HostConfig", "ToolCli", "ToolCliError"]


class ToolCliError(Exception):
    """Команда CLI не разобрана или модуль инструментов не пригоден."""


class CliFlag(StrEnum):
    """Флаги CLI поверх флагов модуля инструментов."""

    CONFIG = "--config"


class HostConfig:
    """Toml приложения при запуске тела на хосте: каталог kerberos и injected-секции."""

    KERBEROS_SECTION: ClassVar[str] = "krb"

    def __init__(self, path: Path) -> None:
        self._raw: DictConfig = AppLayers.compose(path)

    def enter_kerberos(self) -> None:
        """Рабочий каталог kerberos из [krb]; без секции keytab-профили телу закрыты."""
        if self.KERBEROS_SECTION not in self._raw:
            return

        bind(self._raw, self.KERBEROS_SECTION, KerberosWorkspaceConfig).apply()

    def injected(self, fields: Mapping[str, Any]) -> bytes:
        """Injected-модели из секций toml; каждая знает свою секцию SECTION."""
        payload: dict[str, Any] = {}
        for name, annotation in fields.items():
            section = ToolArgv.section_of(name, annotation)
            model = bind(self._raw, section, annotation)
            payload[name] = ToolArgv.reveal(annotation, model)

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ToolCli:
    """argv CLI -> модуль, имя, toml -> ToolMain.run модуля in-process."""

    TOOLS_ATTRIBUTE: ClassVar[str] = "TOOLS"
    INJECTED_FILE: ClassVar[str] = "injected.json"
    USAGE: ClassVar[str] = (
        "usage: python -m boba.runtime.toolcli <module> <tool> [--flags] "
        f"{CliFlag.CONFIG} <toml> [{EntryFlag.INJECTED} <json>]"
    )

    @classmethod
    def main(cls, argv: Sequence[str]) -> int:
        try:
            return cls._run(list(argv))
        except ToolCliError as exc:
            print(f"{exc}\n{cls.USAGE}", file=sys.stderr)  # noqa: T201
            return ToolMain.Exit.ENTRY_ERROR

    @classmethod
    def _run(cls, arguments: list[str]) -> int:
        if len(arguments) < 2:  # noqa: PLR2004
            msg = (
                "toolcli: expected a tool module and a tool name as the first "
                f"two arguments, got {arguments!r}"
            )
            raise ToolCliError(msg)

        module_name = arguments.pop(0)
        tool_name = arguments[0]

        config_path = cls._pop_config(arguments)
        tools = cls._tools_of(module_name)

        host = HostConfig(config_path)
        host.enter_kerberos()

        if EntryFlag.INJECTED in arguments:
            return ToolMain.run(tools, arguments)

        tool = cls._lookup(tools, tool_name)
        schema = ToolArgv.schema_of(tool)
        injected = host.injected(ToolArgv.injected_fields(schema))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / cls.INJECTED_FILE
            path.write_bytes(injected)

            arguments.append(EntryFlag.INJECTED)
            arguments.append(str(path))

            return ToolMain.run(tools, arguments)

    @classmethod
    def _pop_config(cls, arguments: list[str]) -> Path:
        if CliFlag.CONFIG not in arguments:
            msg = (
                f"toolcli: {CliFlag.CONFIG} <toml> is required but missing "
                f"from the arguments {arguments!r}"
            )
            raise ToolCliError(msg)

        index = arguments.index(CliFlag.CONFIG)
        if index + 1 >= len(arguments):
            msg = (
                f"toolcli: {CliFlag.CONFIG} is the last argument, expected a "
                "path after it"
            )
            raise ToolCliError(msg)

        arguments.pop(index)
        raw = arguments.pop(index)

        path = Path(raw)
        if not path.is_file():
            msg = f"toolcli: {CliFlag.CONFIG} expects an existing toml file, got {path}"
            raise ToolCliError(msg)

        return path

    @classmethod
    def _tools_of(cls, module_name: str) -> Sequence[ToolLike]:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            msg = f"toolcli: tool module {module_name!r} is not importable: {exc}"
            raise ToolCliError(msg) from exc

        tools = getattr(module, cls.TOOLS_ATTRIBUTE, None)
        if tools is None:
            msg = (
                f"toolcli: module {module_name!r} declares no "
                f"{cls.TOOLS_ATTRIBUTE} attribute with its tools"
            )
            raise ToolCliError(msg)

        return tools

    @staticmethod
    def _lookup(tools: Sequence[ToolLike], name: str) -> ToolLike:
        for tool in tools:
            if tool.name == name:
                return tool

        known = ", ".join(sorted(tool.name for tool in tools))
        msg = f"toolcli: unknown tool {name!r}; the module declares: {known}"
        raise ToolCliError(msg)


if __name__ == "__main__":
    sys.exit(ToolCli.main(sys.argv[1:]))
