"""Логи вызова инструмента: имя, аргументы, статус и длительность."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ClassVar, cast

from langchain_core.tools import BaseTool

from boba.sandbox.runner import ToolCallContext

__all__ = ["ToolRunLogger"]

logger = logging.getLogger(__name__)


class ToolRunLogger:
    """Пишет start/ok/failed вокруг каждого вызова инструмента."""

    ARGS_LIMIT: ClassVar[int] = 500

    @staticmethod
    def guard_all(tools: list[BaseTool]) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            if callable(func):
                tool.func = ToolRunLogger._wrap(func, tool.name)
            coroutine = cast(
                "Callable[..., Awaitable[object]] | None",
                getattr(tool, "coroutine", None),
            )
            if callable(coroutine):
                tool.coroutine = ToolRunLogger._wrap_async(coroutine, tool.name)
        return tools

    @staticmethod
    def _wrap(func: Callable[..., object], name: str) -> Callable[..., object]:
        @wraps(func)
        def wrapped(*args: object, **kwargs: object) -> object:
            ToolRunLogger._log_start(name, args, kwargs)
            started = time.monotonic()
            token = ToolCallContext.set(name)
            try:
                result = func(*args, **kwargs)
            except Exception as e:
                ToolRunLogger._log_failure(name, started, e)
                raise
            finally:
                ToolCallContext.reset(token)
            ToolRunLogger._log_success(name, started)
            return result

        return wrapped

    @staticmethod
    def _wrap_async(
        coroutine: Callable[..., Awaitable[object]],
        name: str,
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def wrapped(*args: object, **kwargs: object) -> object:
            ToolRunLogger._log_start(name, args, kwargs)
            started = time.monotonic()
            token = ToolCallContext.set(name)
            try:
                result = await coroutine(*args, **kwargs)
            except Exception as e:
                ToolRunLogger._log_failure(name, started, e)
                raise
            finally:
                ToolCallContext.reset(token)
            ToolRunLogger._log_success(name, started)
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
    def _log_success(name: str, started: float) -> None:
        logger.info("tool[%s]: ok in %dms", name, ToolRunLogger._elapsed_ms(started))

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
