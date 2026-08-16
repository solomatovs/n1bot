"""Логи вызова инструмента и журнал его живого вывода.

Обвязка знает вызов целиком: имя, tool_call_id из синтетического поля схемы
(ToolCallIdField), исход и длительность. Поэтому она же открывает журнал
живого вывода через переданный stream_source, ставит приёмники каналов в тапы
исполнителя и закрывает журнал по исходу вызова.

Ошибки: своих не выпускает; исключение тела проходит наверх как есть.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Sequence
from contextvars import Token
from dataclasses import dataclass
from typing import ClassVar, Protocol, TypeAlias

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody
from boba.toolkit.channels import CallOutcome, ToolChannel
from boba.toolkit.failure import FailureText
from boba.toolkit.result import ToolResultBase
from boba.toolkit.stream import (
    StreamSink,
    ToolCallContext,
    ToolCallInfo,
    ToolChannelsTap,
    ToolStreamTap,
)

__all__ = ["CallStream", "StreamSource", "ToolRunLogger"]

logger = logging.getLogger(__name__)


class CallStream(Protocol):
    """Журнал живого вывода одного вызова: приёмники каналов и закрытие."""

    @abstractmethod
    def sink_of(self, channel: ToolChannel) -> StreamSink: ...

    @abstractmethod
    def close(self, note: str) -> None: ...


StreamSource: TypeAlias = Callable[[str, str], "CallStream | None"]
"""(имя инструмента, call_id) -> журнал вызова; None — вызов не потоковый."""


@dataclass
class _CallScope:
    """Контекст одного вызова: журнал, таймер и исход для закрытия журнала."""

    name: str
    started: float
    token: Token[ToolCallInfo | None]
    stream: CallStream | None
    note: str


class ToolRunLogger:
    """Пишет start/ok/failed вокруг вызова и ведёт журнал живого вывода."""

    ARGS_LIMIT: ClassVar[int] = 500

    PACKED_RESULT: ClassVar[int] = 2
    """Длина кортежа (content, artifact) у tool'ов с content_and_artifact."""

    class _Hooks(CallHooks["_CallScope"]):
        def __init__(self, stream_source: StreamSource) -> None:
            self._stream_source = stream_source

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> _CallScope:
            call_id = ToolCallIdField.pop(kwargs)
            ToolRunLogger._log_start(name, args, kwargs)

            token = ToolCallContext.set(ToolCallInfo(name=name, call_id=call_id))
            stream = ToolRunLogger._open_stream(name, call_id, self._stream_source)
            if stream is not None:
                # оба тапа: текстовый запуск читает stdout-приёмник, канальный
                # берёт приёмники всех каналов из ToolChannelsTap
                ToolStreamTap.set(stream.sink_of(ToolChannel.STDOUT))
                ToolChannelsTap.set(stream)

            return _CallScope(
                name=name,
                started=time.monotonic(),
                token=token,
                stream=stream,
                note=str(CallOutcome.FAILED),
            )

        def after(self, ctx: _CallScope, result: object) -> object:
            ctx.note = str(CallOutcome.FINISHED)
            ToolRunLogger._log_outcome(ctx.name, ctx.started, result)
            return result

        def on_error(self, ctx: _CallScope, error: Exception) -> object:
            ToolRunLogger._log_failure(ctx.name, ctx.started, error)
            raise error

        def cleanup(self, ctx: _CallScope) -> None:
            if ctx.stream is not None:
                ToolStreamTap.set(None)
                ToolChannelsTap.set(None)
                ctx.stream.close(ctx.note)

            ToolCallContext.reset(ctx.token)

    @classmethod
    def guard_all(
        cls,
        tools: Sequence[BaseTool],
        stream_source: StreamSource,
    ) -> list[BaseTool]:
        return ToolBody.hook_all(tools, cls._Hooks(stream_source))

    @staticmethod
    def _open_stream(
        name: str, call_id: str, stream_source: StreamSource
    ) -> CallStream | None:
        """Журнал вызова; без call_id или не потоковому инструменту — нет."""
        if not call_id:
            return None

        return stream_source(name, call_id)

    @classmethod
    def _log_start(
        cls,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        logger.info("tool[%s]: start args=%s", name, cls._render_args(args, kwargs))

    @staticmethod
    def _log_outcome(name: str, started: float, result: object) -> None:
        """Инструмент мог вернуть отказ вместо исключения — это не «ok»."""
        elapsed = ToolRunLogger._elapsed_ms(started)
        failure = ToolRunLogger._reported_failure(result)
        if failure is None:
            logger.info("tool[%s]: ok in %dms", name, elapsed)
            return
        logger.warning("tool[%s]: failed in %dms: %s", name, elapsed, failure)

    @staticmethod
    def _reported_failure(result: object) -> str | None:
        """Текст отказа из ToolResult; None — инструмент отработал успешно."""
        payload = result
        if isinstance(payload, tuple) and len(payload) == ToolRunLogger.PACKED_RESULT:
            payload = payload[1]

        if not isinstance(payload, ToolResultBase):
            return None

        if payload.ok:
            return None

        return payload.llm_text()

    @staticmethod
    def _log_failure(name: str, started: float, error: BaseException) -> None:
        """Тот же текст уходит LLM в ErrorResult: лог и история совпадают."""
        logger.warning(
            "tool[%s]: failed in %dms: %s",
            name,
            ToolRunLogger._elapsed_ms(started),
            FailureText.of(error),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @classmethod
    def _render_args(
        cls,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> str:
        parts: list[str] = []
        for value in args:
            parts.append(repr(value))
        for key, value in kwargs.items():
            parts.append(f"{key}={value!r}")
        text = ", ".join(parts)
        if len(text) > cls.ARGS_LIMIT:
            return text[: cls.ARGS_LIMIT] + "…"
        return text
