"""Логи вызова инструмента: имя, аргументы, статус и длительность."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from functools import wraps
from typing import ClassVar, cast

from langchain_core.tools import BaseTool

from boba.chainlit.agent.tools.wrapping import AsyncCall, SyncCall, ToolBody
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.result import ToolResult, ToolResultBase, render_for_llm

__all__ = ["ToolRunLogger"]

logger = logging.getLogger(__name__)


class ToolRunLogger:
    """Пишет start/ok/failed вокруг каждого вызова инструмента."""

    ARGS_LIMIT: ClassVar[int] = 500

    PACKED_RESULT: ClassVar[int] = 2
    """Длина кортежа (content, artifact) у tool'ов с content_and_artifact."""

    @staticmethod
    def guard_all(tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.wrap_all(
            tools, ToolRunLogger._wrap, ToolRunLogger._wrap_async
        )

    @staticmethod
    def _wrap(call: SyncCall, name: str) -> SyncCall:
        @wraps(call)
        def wrapped(*args: object, **kwargs: object) -> object:
            ToolRunLogger._log_start(name, args, kwargs)
            started = time.monotonic()
            token = ToolCallContext.set(name)
            try:
                result = call(*args, **kwargs)
            except Exception as e:
                ToolRunLogger._log_failure(name, started, e)
                raise
            finally:
                ToolCallContext.reset(token)
            ToolRunLogger._log_outcome(name, started, result)
            return result

        return wrapped

    @staticmethod
    def _wrap_async(call: AsyncCall, name: str) -> AsyncCall:
        @wraps(call)
        async def wrapped(*args: object, **kwargs: object) -> object:
            ToolRunLogger._log_start(name, args, kwargs)
            started = time.monotonic()
            token = ToolCallContext.set(name)
            try:
                result = await call(*args, **kwargs)
            except Exception as e:
                ToolRunLogger._log_failure(name, started, e)
                raise
            finally:
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
