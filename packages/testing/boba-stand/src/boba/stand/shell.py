"""Shell-команда в песочнице для тестов: через модульный bash-тул, как в бою.

Ошибки:
LauncherError — вызов умер без конверта: команду убил сигнал или таймаут
    вызова; текст несёт код возврата и диагностику профиля.
PayloadFailureError — тело отказало конвертом ошибки.
"""

from __future__ import annotations

from typing import ClassVar

from langchain_core.tools import BaseTool

from boba.runtime.plugins import ToolBridge
from boba.tool.shell.tools import TOOLS, BashToolConfig
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import CollectedCall, PayloadFailureError, ToolLauncher
from boba.toolkit.protocol import ReplyError
from boba.toolkit.result import ShellResult
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.injected import InjectedConfig

__all__ = ["ShellRun"]


class ShellRun:
    """Запуск команды bash-тулом через ToolLauncher стенда."""

    MODULE: ClassVar[str] = "boba.tool.shell.tools"
    """Модуль тела: зигота стенда грузит его, чтобы команды было кому исполнять."""

    CONFIG: ClassVar[BashToolConfig] = BashToolConfig(
        max_output_bytes=1 << 20, timeout_sec=300.0
    )

    @classmethod
    def tool(cls, launcher: ToolLauncher, cfg: BashToolConfig = CONFIG) -> BaseTool:
        """Langchain-тул bash поверх launcher'а: обёртка запуска и конфиг,
        как ставит загрузчик приложения."""
        payload = TOOLS[0]
        if not isinstance(payload, PayloadTool):
            msg = f"bash TOOLS[0] is {type(payload).__name__}, PayloadTool expected"
            raise PayloadFailureError("contract", msg)

        copy = payload.model_copy()
        bridged = ToolBridge.as_structured_tool(copy)
        ToolProcessWrap.guard_all(ToolMain.toolset(bridged), launcher)
        InjectedConfig.bind_all([bridged], lambda name, annotation: cfg)

        return bridged

    @classmethod
    def call_text(
        cls,
        launcher: ToolLauncher,
        command: str,
        cfg: BashToolConfig = CONFIG,
    ) -> ShellResult:
        tool = TOOLS[0]
        rendered = ToolArgv.render(
            ToolAddress.of(tool), tool.args_schema, {"command": command, "cfg": cfg}
        )

        outcome = CollectedCall.of(launcher, rendered)
        reply = outcome.reply
        if isinstance(reply, ReplyError):
            raise PayloadFailureError(reply.kind, reply.message)

        artifact = reply.artifact
        if not isinstance(artifact, ShellResult):
            msg = f"bash returned {type(artifact).__name__}, ShellResult expected"
            raise PayloadFailureError("contract", msg)

        return artifact
