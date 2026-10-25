"""Зигота фейкового плагина для тестов: команда программы, отпечаток
манифеста, секция прогрева и команды вызова его инструментов."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any, ClassVar

from fake_channel_tool import PLUGIN

from boba.sandbox.profile import SandboxProfile
from boba.sandbox.program import PluginCommand, PluginEntry
from boba.sandbox.zygote import ZygotePolicy, ZygoteSection
from boba.stand.zygote import ZygoteStand
from boba.toolkit.entry import ToolArgv
from boba.toolkit.facade import PayloadTool
from boba.toolkit.manifest import ManifestBuilder
from boba.toolkit.protocol import ToolCommand


class FxZygote:
    """Фейковый плагин fx глазами хоста: зигота поднимается его программой
    по ссылке на объявление, отпечаток манифеста хост считает сам, вызовы
    кодируются по объявлениям манифеста, как у обёртки запуска."""

    REF: ClassVar[str] = "fake_channel_tool:PLUGIN"
    WARMUP: ClassVar[Mapping[str, Any]] = MappingProxyType({"greeting": "privet"})
    """Секция плагина для прогрева warm_cache: без неё зигота не поднимается."""

    def __init__(self) -> None:
        self._entry = PluginEntry()
        self._builder = ManifestBuilder()

    def program(self, python: str) -> tuple[str, ...]:
        """Команда зиготы fx интерпретатором python; ZygoteArgs — за ней."""
        argv = self._entry.declared_argv(python, [self.REF])

        return (*argv, PluginCommand.ZYGOTE.value)

    def command(self, python: str, command: PluginCommand) -> tuple[str, ...]:
        """Подкоманда command программы fx интерпретатором python."""
        return (*self._entry.declared_argv(python, [self.REF]), command.value)

    def digest(self) -> str:
        """Отпечаток манифеста fx, который зигота называет при готовности."""
        manifest = self._builder.manifest(PLUGIN, PluginEntry.UNKNOWN_VERSION)

        return manifest.digest()

    def section(
        self,
        stand: ZygoteStand,
        name: str,
        profile: SandboxProfile,
        policy: ZygotePolicy,
        warmup: Mapping[str, Any] = WARMUP,
    ) -> ZygoteSection:
        """Секция реестра стенда: fx рядом с bash-тулом стенда, политика
        policy теста."""
        section = stand.section(name, profile, [self.REF], warmup)

        return replace(section, policy=policy)

    def args(self, kwargs: Mapping[str, Any]) -> bytes:
        """Аргументы вызова JSON-объектом: так их шлёт обёртка запуска."""
        return json.dumps(dict(kwargs)).encode("utf-8")

    def render(
        self,
        tool: str,
        kwargs: Mapping[str, object],
        input_counts: Mapping[str, int] = MappingProxyType({}),
        output_counts: Mapping[str, int] = MappingProxyType({}),
    ) -> ToolCommand:
        """Команда вызова инструмента fx по его объявлению в манифесте."""
        return ToolArgv.render(
            self._builder.tool(self._payload(tool)), kwargs, input_counts, output_counts
        )

    def _payload(self, tool: str) -> PayloadTool:
        names: list[str] = []
        for payload in PLUGIN.tools:
            if payload.name == tool:
                return payload

            names.append(payload.name)

        msg = f"fake plugin fx has no tool {tool!r}: {names}"
        raise KeyError(msg)
