"""Тап живого вывода вокруг вызова инструмента.

Обвязка работает в контексте исполнения тула — колбэки langchain для
sync-инструментов едут в чужом потоке и передать contextvar не могут.
Приёмник своего вызова выдаёт источник, переданный при постановке обвязки,
и на время вызова становится тапом; песочница пишет в него по мере чтения
процесса.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from functools import partial, wraps
from typing import TypeAlias

from langchain_core.tools import BaseTool

from boba.chainlit.agent.toolrun.wrapping import AsyncCall, SyncCall, ToolBody
from boba.toolkit.stream import StreamSink, ToolStreamTap

__all__ = ["TapSource", "ToolStreamTapGuard"]

TapSource: TypeAlias = Callable[[str], StreamSink | None]
"""Приёмник живого вывода для вызова инструмента; None — вызов не потоковый."""


class ToolStreamTapGuard:
    """Ставит окно живого вывода в контекст каждого вызова инструмента."""

    @staticmethod
    def guard_all(
        tools: Sequence[BaseTool],
        tap_source: TapSource,
    ) -> list[BaseTool]:
        wrap = partial(ToolStreamTapGuard._wrap, tap_source=tap_source)
        wrap_async = partial(ToolStreamTapGuard._wrap_async, tap_source=tap_source)

        return ToolBody.wrap_all(tools, wrap, wrap_async)

    @staticmethod
    def _claim(name: str, tap_source: TapSource) -> bool:
        """Забрать приёмник своего вызова в тап; False — вызов не потоковый."""
        sink = tap_source(name)
        if sink is None:
            return False

        ToolStreamTap.set(sink)
        return True

    @staticmethod
    def _wrap(call: SyncCall, name: str, tap_source: TapSource) -> SyncCall:
        @wraps(call)
        def wrapped(*args: object, **kwargs: object) -> object:
            claimed = ToolStreamTapGuard._claim(name, tap_source)
            try:
                return call(*args, **kwargs)
            finally:
                if claimed:
                    ToolStreamTap.set(None)

        return wrapped

    @staticmethod
    def _wrap_async(call: AsyncCall, name: str, tap_source: TapSource) -> AsyncCall:
        @wraps(call)
        async def wrapped(*args: object, **kwargs: object) -> object:
            claimed = ToolStreamTapGuard._claim(name, tap_source)
            try:
                return await call(*args, **kwargs)
            finally:
                if claimed:
                    ToolStreamTap.set(None)

        return wrapped
