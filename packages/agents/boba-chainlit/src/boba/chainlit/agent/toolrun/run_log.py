"""Логи вызова инструмента и журнал его живого вывода.

Обвязка знает вызов целиком: имя, tool_call_id из синтетического поля схемы
(ToolCallIdField), исход и длительность. Поэтому она же открывает журнал
живого вывода через переданный stream_source, ставит приёмник канала stdout
в тап исполнителя и закрывает журнал по исходу вызова.

Ошибки: своих не выпускает; исключение тела проходит наверх как есть.
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Sequence
from enum import StrEnum
from functools import partial, wraps
from typing import ClassVar, Protocol, TypeAlias, cast

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.wrapping import AsyncCall, SyncCall, ToolBody
from boba.toolkit.channels import ToolChannel
from boba.toolkit.result import ToolResult, ToolResultBase, render_for_llm
from boba.toolkit.stream import (
    StreamSink,
    ToolCallContext,
    ToolCallInfo,
    ToolStreamTap,
)

__all__ = ["CallNote", "CallStream", "StreamSource", "ToolRunLogger"]

logger = logging.getLogger(__name__)


class CallNote(StrEnum):
    """Итог журнала вызова; значения совпадают с формулировками панели."""

    FINISHED = "finished"
    FAILED = "failed"


class CallStream(Protocol):
    """Журнал живого вывода одного вызова: приёмники каналов и закрытие."""

    @abstractmethod
    def sink_of(self, channel: ToolChannel) -> StreamSink: ...

    @abstractmethod
    def close(self, note: str) -> None: ...


StreamSource: TypeAlias = Callable[[str, str], "CallStream | None"]
"""(имя инструмента, call_id) -> журнал вызова; None — вызов не потоковый."""


class ToolRunLogger:
    """Пишет start/ok/failed вокруг вызова и ведёт журнал живого вывода."""

    ARGS_LIMIT: ClassVar[int] = 500

    PACKED_RESULT: ClassVar[int] = 2
    """Длина кортежа (content, artifact) у tool'ов с content_and_artifact."""

    @staticmethod
    def guard_all(
        tools: Sequence[BaseTool],
        stream_source: StreamSource,
    ) -> list[BaseTool]:
        wrap = partial(ToolRunLogger._wrap, stream_source=stream_source)
        wrap_async = partial(ToolRunLogger._wrap_async, stream_source=stream_source)

        return ToolBody.wrap_all(tools, wrap, wrap_async)

    @staticmethod
    def _open_stream(
        name: str, call_id: str, stream_source: StreamSource
    ) -> CallStream | None:
        """Журнал вызова; без call_id или не потоковому инструменту — нет."""
        if not call_id:
            return None

        return stream_source(name, call_id)

    @staticmethod
    def _wrap(call: SyncCall, name: str, stream_source: StreamSource) -> SyncCall:
        @wraps(call)
        def wrapped(*args: object, **kwargs: object) -> object:
            call_id = ToolCallIdField.pop(kwargs)
            ToolRunLogger._log_start(name, args, kwargs)

            started = time.monotonic()
            token = ToolCallContext.set(ToolCallInfo(name=name, call_id=call_id))
            stream = ToolRunLogger._open_stream(name, call_id, stream_source)
            if stream is not None:
                ToolStreamTap.set(stream.sink_of(ToolChannel.STDOUT))

            note = CallNote.FAILED
            try:
                result = call(*args, **kwargs)
                note = CallNote.FINISHED
            except Exception as e:
                ToolRunLogger._log_failure(name, started, e)
                raise
            finally:
                if stream is not None:
                    ToolStreamTap.set(None)
                    stream.close(str(note))
                ToolCallContext.reset(token)

            ToolRunLogger._log_outcome(name, started, result)
            return result

        return wrapped

    @staticmethod
    def _wrap_async(
        call: AsyncCall, name: str, stream_source: StreamSource
    ) -> AsyncCall:
        @wraps(call)
        async def wrapped(*args: object, **kwargs: object) -> object:
            call_id = ToolCallIdField.pop(kwargs)
            ToolRunLogger._log_start(name, args, kwargs)

            started = time.monotonic()
            token = ToolCallContext.set(ToolCallInfo(name=name, call_id=call_id))
            stream = ToolRunLogger._open_stream(name, call_id, stream_source)
            if stream is not None:
                ToolStreamTap.set(stream.sink_of(ToolChannel.STDOUT))

            note = CallNote.FAILED
            try:
                result = await call(*args, **kwargs)
                note = CallNote.FINISHED
            except Exception as e:
                ToolRunLogger._log_failure(name, started, e)
                raise
            finally:
                if stream is not None:
                    ToolStreamTap.set(None)
                    stream.close(str(note))
                ToolCallContext.reset(token)

            ToolRunLogger._log_outcome(name, started, result)
            return result

        return wrapped

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
        if not isinstance(payload, ToolResultBase) or payload.ok:
            return None
        return render_for_llm(cast("ToolResult", payload))

    @staticmethod
    def _log_failure(name: str, started: float, error: BaseException) -> None:
        logger.warning(
            "tool[%s]: failed in %dms: %s: %s",
            name,
            ToolRunLogger._elapsed_ms(started),
            type(error).__name__,
            error,
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
