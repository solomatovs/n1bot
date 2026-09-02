"""Модели границы процесса инструмента: команда запуска и конверт ответа.

Хост и тело инструмента живут в разных процессах и общаются данными, а не
вызовами. Хост собирает ToolCommand (argv плюс injected-конфиг) и запускает
её через ToolLauncher; тело по завершении пишет конверт ReplyOk/ReplyError
в канал результата, хост разбирает его через EnvelopeReply. Обе стороны
зависят от этого модуля и не зависят друг от друга.

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
    """Что запускать: argv команды модуля инструментов и injected-конфиг тела.

    Собирается хостом (ToolArgv.render) из аргументов LLM и injected-моделей;
    конфиг с секретами в argv не попадает — лончер отправляет его телу
    отдельным каналом --injected-fd.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...]
    config: bytes


class ReplyOk(BaseModel):
    """Конверт успешного вызова: content для LLM и артефакт результата,
    возвращённые телом через границу процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    content: str
    artifact: ToolResult


class ReplyError(BaseModel):
    """Конверт отказа: kind и сообщение ожидаемой ошибки тела либо нарушения
    контракта запуска; текст пригоден для показа пользователю."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["error"] = "error"
    kind: str
    message: str


ToolReply = Annotated[ReplyOk | ReplyError, Field(discriminator="status")]
REPLY: Final[TypeAdapter[ReplyOk | ReplyError]] = TypeAdapter(ToolReply)
