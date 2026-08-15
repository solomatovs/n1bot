"""Каналы одного вызова инструмента: имена, направление, env-реестр.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ToolChannel", "WrapChannel"]


class ToolChannel(StrEnum):
    """Каналы тела инструмента; значение — имя канала в файле журнала."""

    STDIN = "tool_stdin"
    STDOUT = "tool_stdout"
    STDERR = "tool_stderr"
    RESULT = "tool_result"

    @property
    def env_name(self) -> str:
        """Имя env-переменной с номером fd: BOBA_FD_TOOL_RESULT."""
        return f"BOBA_FD_{self.name}"

    @property
    def inbound(self) -> bool:
        """Канал ведёт в процесс инструмента, а не из него."""
        return self is ToolChannel.STDIN


class WrapChannel(StrEnum):
    """Каналы обвязки: stdout/stderr самого процесса bwrap."""

    STDOUT = "wrap_stdout"
    STDERR = "wrap_stderr"
