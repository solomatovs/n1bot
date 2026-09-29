"""Модели границы процесса инструмента: команда запуска и конверт ответа.

Хост и тело инструмента живут в разных процессах и общаются данными, а не
вызовами. Хост собирает ToolCommand (argv плюс injected-конфиг) и запускает
её через ToolLauncher; тело по завершении пишет конверт ReplyOk/ReplyError
в канал результата, хост разбирает его через EnvelopeReply. Обе стороны
зависят от этого модуля и не зависят друг от друга.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from boba.toolkit.result import FailureResultField, ToolResult

__all__ = [
    "REPLY",
    "CallGateMode",
    "CallInputSpec",
    "ReplyError",
    "ReplyOk",
    "ToolCommand",
    "ToolReply",
]


class CallGateMode(StrEnum):
    """Как хост отвечает на барьер тела (StreamGroup.ready).

    AUTO — сразу разрешает: вызов вне группы, ждать некого. HELD — держит
    ответ, пока группа связанных вызовов не решит, всем ли можно
    фиксировать результат.
    """

    AUTO = "auto"
    HELD = "held"


class CallInputSpec(BaseModel):
    """Один вход вызова: какому входному порту тела он принадлежит и сырой ли.

    Входной порт тела — одиночный Inbound (ровно один вход) либо список
    Sequence[Inbound] (сколько входов назвал вызывающий); у каждого входа
    свой пайп. raw — порт голых байтов: лончер такой вход не кадрирует и не
    журналирует.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str = Field(min_length=1)
    raw: bool


class ToolCommand(BaseModel):
    """Что запускать: argv команды модуля инструментов и injected-конфиг тела.

    Собирается хостом (ToolArgv.render) из аргументов LLM и injected-моделей;
    конфиг с секретами в argv не попадает — лончер отправляет его телу
    отдельным каналом --injected-fd. inputs — входы вызова по порядку, у
    каждого свой пайп и флаг --fd-in; первый едет по stdin процесса.
    raw_frames выводится из декларации выходного порта: сырой канал несёт
    голые байты, лончер его не разбирает и не журналирует. gate — режим
    барьера тела: вне группы хост отвечает на него сразу.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    MAX_INPUTS: ClassVar[int] = 32
    """Потолок входов вызова: каждый — пайп, а в песочницу дескрипторы едут
    одним сообщением SCM_RIGHTS."""

    argv: tuple[str, ...]
    config: bytes
    inputs: tuple[CallInputSpec, ...] = Field(default=(), max_length=MAX_INPUTS)
    raw_frames: bool = False
    gate: CallGateMode = CallGateMode.AUTO


class ReplyOk(BaseModel):
    """Конверт успешного вызова: content для LLM и артефакт результата,
    возвращённые телом через границу процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = "ok"
    content: str
    artifact: ToolResult


class ReplyError(BaseModel):
    """Конверт ошибки: любое исключение тела или нарушение контракта запуска,
    упакованное в результат-ошибку (FailurePacker) по ту сторону процесса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["error"] = "error"
    failure: FailureResultField


ToolReply = Annotated[ReplyOk | ReplyError, Field(discriminator="status")]
REPLY: Final[TypeAdapter[ReplyOk | ReplyError]] = TypeAdapter(ToolReply)
