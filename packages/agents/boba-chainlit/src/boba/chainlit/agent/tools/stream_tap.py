"""Тап живого вывода вокруг вызова инструмента.

Обвязка работает в контексте исполнения тула — колбэки langchain для
sync-инструментов едут в чужом потоке и передать contextvar не могут.
Стрим своего вызова забирается из очереди реестра и на время вызова
становится тапом; песочница пишет в него по мере чтения процесса.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import cast

from langchain_core.tools import BaseTool

from boba.chainlit.infra.session import current_thread_id
from boba.chainlit.rendering.stream_view import ToolStreams
from boba.toolkit.stream import ToolStreamTap

__all__ = ["ToolStreamTapGuard"]


class ToolStreamTapGuard:
    """Ставит окно живого вывода в контекст каждого вызова инструмента."""

    @staticmethod
    def guard_all(tools: list[BaseTool]) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            if callable(func):
                tool.func = ToolStreamTapGuard._wrap(func, tool.name)
            coroutine = cast(
                "Callable[..., Awaitable[object]] | None",
                getattr(tool, "coroutine", None),
            )
            if callable(coroutine):
                tool.coroutine = ToolStreamTapGuard._wrap_async(
                    coroutine, tool.name
                )
        return tools

    @staticmethod
    def _claim(name: str) -> bool:
        """Забрать стрим своего вызова в тап; False — вызов не потоковый."""
        thread_id = current_thread_id()
        if thread_id is None:
            return False

        stream = ToolStreams.claim(thread_id, name)
        if stream is None:
            return False

        ToolStreamTap.set(stream.buffer)
        return True

    @staticmethod
    def _wrap(func: Callable[..., object], name: str) -> Callable[..., object]:
        @wraps(func)
        def wrapped(*args: object, **kwargs: object) -> object:
            claimed = ToolStreamTapGuard._claim(name)
            try:
                return func(*args, **kwargs)
            finally:
                if claimed:
                    ToolStreamTap.set(None)

        return wrapped

    @staticmethod
    def _wrap_async(
        coroutine: Callable[..., Awaitable[object]],
        name: str,
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def wrapped(*args: object, **kwargs: object) -> object:
            claimed = ToolStreamTapGuard._claim(name)
            try:
                return await coroutine(*args, **kwargs)
            finally:
                if claimed:
                    ToolStreamTap.set(None)

        return wrapped
