"""Сборка bash-фасада для тестов: реестр стадий из одного узла и порт запуска.

Обвязка узла bash — обычный python-пакет, поэтому профиль теста дополняется
монтированиями её кода и зависимостей: без них внутри песочницы нет ни boba,
ни pydantic.

Вне приложения карты прав нет: реестр собран под сам тест, чужих узлов в нём
нет, поэтому предикат разрешает всё.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar

import pydantic
from langchain_core.tools import BaseTool

from boba.sandbox import SandboxCaller
from boba.sandbox.profile import BindSpec, SandboxProfile
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.stand.journal import CallStand
from boba.tool.shell import BashStage, build_bash_tool
from boba.toolkit.launcher import LauncherFactory, ToolLauncher

REPO = Path(__file__).resolve().parents[4]


class AllowAllNodes:
    """Права вне сессии: в реестре теста только его собственные узлы."""

    def __call__(self, tool: str, /) -> bool:
        return True


class BashPayloadMounts:
    """Код обвязки и её зависимости внутри песочницы: монтирования и env."""

    SOURCES: ClassVar[Mapping[str, str]] = {
        str(REPO / "packages" / "core" / "boba-toolkit" / "src"): "/opt/toolkit",
        str(REPO / "packages" / "tools" / "boba-tool-shell" / "src"): "/opt/shell",
        str(Path(pydantic.__file__).resolve().parents[1]): "/opt/site",
    }

    PATH: ClassVar[str] = "/usr/local/bin:/usr/bin:/bin"
    """Интерпретатор обвязки — тот же, на котором собраны site-packages теста."""

    @classmethod
    def applied(cls, profile: SandboxProfile) -> SandboxProfile:
        """Профиль плюс монтирования обвязки и её PATH/PYTHONPATH."""
        binds = list(profile.ro_binds)
        for host, target in cls.SOURCES.items():
            binds.append(BindSpec(host=host, target=target))

        env = dict(profile.env_set)
        env["PYTHONPATH"] = ":".join(cls.SOURCES.values())
        env["PATH"] = cls.PATH

        return profile.model_copy(
            update={"ro_binds": tuple(binds), "env_set": env},
        )


class BashStageSetup:
    """Узел bash с профилем теста: реестр, исполнитель и готовый фасад."""

    @staticmethod
    def registry(profile: SandboxProfile) -> StageRegistry:
        defs: dict[str, StageDef] = {}
        for name, node in BashStage.stages().items():
            defs[name] = StageDef.of(node, profile)

        return StageRegistry(defs)

    @classmethod
    def launchers(
        cls,
        profile: SandboxProfile,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> LauncherFactory:
        caller = SandboxCaller(
            cls.registry(profile),
            AllowAllNodes(),
            path_vars,
            CallStand.journal(),
        )

        def launcher(tool: str) -> ToolLauncher:
            return caller

        return launcher

    @classmethod
    def tool(
        cls,
        profile: SandboxProfile,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> BaseTool:
        ready = BashPayloadMounts.applied(profile)

        return build_bash_tool(
            cls.launchers(ready, path_vars), ready.max_output_bytes
        )
