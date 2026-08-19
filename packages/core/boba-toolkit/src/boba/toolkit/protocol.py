"""Протокол обмена с процессом инструмента: команда запуска и конверт ответа.

Модуль общий для обеих сторон границы процесса: entry исполняет тело и пишет
конверт, launcher запускает команду и разбирает его. Оба зависят отсюда, друг
от друга — нет.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from boba.toolkit.result import ToolResult

__all__ = [
    "REPLY",
    "ReplyError",
    "ReplyOk",
    "ToolCommand",
    "ToolReply",
]


class ToolCommand(BaseModel):
    """Что запускать: готовая команда и её stdin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...]
    stdin: bytes


class ReplyOk(BaseModel):
    """Конверт успеха: возвращённое телом значение через границу процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    content: str
    artifact: ToolResult


class ReplyError(BaseModel):
    """Конверт отказа: ожидаемая ошибка тела либо нарушение контракта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["error"] = "error"
    kind: str
    message: str


ToolReply = Annotated[ReplyOk | ReplyError, Field(discriminator="status")]
REPLY: Final[TypeAdapter[ReplyOk | ReplyError]] = TypeAdapter(ToolReply)
