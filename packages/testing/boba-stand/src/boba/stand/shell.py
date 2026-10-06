"""Shell-команда в песочнице для тестов: через модульный bash-тул, как в бою.

Ошибки:
LauncherError — вызов умер без конверта: команду убил сигнал или таймаут
    вызова; текст несёт код возврата и диагностику профиля.
PayloadFailureError — тело отказало конвертом ошибки.
"""

from __future__ import annotations

from typing import ClassVar

from boba.identity.context import CallContexts
from boba.stand.refs import StandRefs
from boba.tool.shell.tools import TOOLS, BashToolConfig
from boba.toolkit.entry import ToolAddress, ToolArgv
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import CollectedCall, PayloadFailureError, ToolLauncher
from boba.toolkit.protocol import ReplyError
from boba.toolkit.result import ErrorResult, ShellResult
from boba.toolrun.hosted import HostedTool
from boba.toolrun.injected import InjectedConfig, StaticConfig

__all__ = ["ShellRun"]


class ShellRun:
    """Запуск команды bash-тулом через ToolLauncher стенда."""

    MODULE: ClassVar[str] = "boba.tool.shell.tools"
    """Модуль тела: зигота стенда грузит его, чтобы команды было кому исполнять."""

    CONTRACT: ClassVar[str] = "contract"
    """error_kind отказа стенда: bash-тул нарушил ожидаемый контракт."""

    CONFIG: ClassVar[BashToolConfig] = BashToolConfig(
        max_output_bytes=1 << 20, timeout_sec=300.0
    )

    @classmethod
    def tool(cls, launcher: ToolLauncher, cfg: BashToolConfig = CONFIG) -> HostedTool:
        """Инструмент хоста bash поверх launcher'а: обёртка запуска и конфиг,
        как ставит загрузчик приложения."""
        payload = TOOLS[0]
        if not isinstance(payload, PayloadTool):
            msg = f"bash TOOLS[0] is {type(payload).__name__}, PayloadTool expected"
            raise PayloadFailureError(ErrorResult(message=msg, error_kind=cls.CONTRACT))

        config = InjectedConfig(lambda name, annotation: cfg, StaticConfig())
        stand = StandRefs(CallContexts()).tool_stand((config,))
        (bridged,) = stand.launch([payload], launcher)

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
            ToolAddress.of(tool),
            tool.args_schema,
            {"command": command, "cfg": cfg},
            input_counts={},
            output_counts={},
        )

        outcome = CollectedCall.of(launcher, rendered)
        reply = outcome.reply
        if isinstance(reply, ReplyError):
            raise PayloadFailureError(reply.failure)

        artifact = reply.artifact
        if not isinstance(artifact, ShellResult):
            msg = f"bash returned {type(artifact).__name__}, ShellResult expected"
            raise PayloadFailureError(ErrorResult(message=msg, error_kind=cls.CONTRACT))

        return artifact
