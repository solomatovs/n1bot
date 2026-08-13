"""Тап живого вывода вокруг вызова инструмента.

Обвязка работает в контексте исполнения тула — колбэки langchain для
sync-инструментов едут в чужом потоке и передать contextvar не могут.
Стрим своего вызова забирается из очереди реестра и на время вызова
становится тапом; песочница пишет в него по мере чтения процесса.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import wraps

from langchain_core.tools import BaseTool

from boba.chainlit.agent.tools.wrapping import AsyncCall, SyncCall, ToolBody
from boba.chainlit.domain.session import current_thread_id
from boba.chainlit.rendering.stream_view import ToolStreams
from boba.toolkit.stream import ToolStreamTap

__all__ = ["ToolStreamTapGuard"]


class ToolStreamTapGuard:
    """Ставит окно живого вывода в контекст каждого вызова инструмента."""

    @staticmethod
    def guard_all(tools: Sequence[BaseTool]) -> list[BaseTool]:
        return ToolBody.wrap_all(
            tools, ToolStreamTapGuard._wrap, ToolStreamTapGuard._wrap_async
        )

    @staticmethod
    def _claim(name: str) -> bool:
        """Забрать стрим своего вызова в тап; False — вызов не потоковый."""
        thread_id = current_thread_id()
        if thread_id is None:
            return False

        stream = ToolStreams.claim(thread_id, name)
        if stream is None:
            return False

        ToolStreamTap.set(stream.recorder)
        return True

    @staticmethod
    def _wrap(call: SyncCall, name: str) -> SyncCall:
        @wraps(call)
        def wrapped(*args: object, **kwargs: object) -> object:
            claimed = ToolStreamTapGuard._claim(name)
            try:
                return call(*args, **kwargs)
            finally:
                if claimed:
                    ToolStreamTap.set(None)

        return wrapped

    @staticmethod
    def _wrap_async(call: AsyncCall, name: str) -> AsyncCall:
        @wraps(call)
        async def wrapped(*args: object, **kwargs: object) -> object:
            claimed = ToolStreamTapGuard._claim(name)
            try:
                return await call(*args, **kwargs)
            finally:
                if claimed:
                    ToolStreamTap.set(None)

        return wrapped
