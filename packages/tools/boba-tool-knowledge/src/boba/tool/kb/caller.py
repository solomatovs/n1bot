"""Вызов kb-payload'а: эмбеддинг и SQL идут внутри песочницы."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import JsonValue

from boba.tool.kb.protocol import KbNode
from boba.toolkit.channels import ChannelSink
from boba.toolkit.launcher import LauncherFactory, StageRun

__all__ = ["KbCaller"]


class KbCaller:
    """Один запуск узла поиска: модель эмбеддера грузится внутри заново."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def search(  # noqa: PLR0913 — параметры поиска независимы
        self,
        *,
        node: KbNode,
        collections: Sequence[str],
        query: str,
        top_k: int,
        snippet_chars: int,
        sink: ChannelSink,
    ) -> None:
        """Строки выдачи уходят в sink; квитанция узла пуста."""
        args: dict[str, JsonValue] = {
            "collections": list(collections),
            "query": query,
            "top_k": top_k,
            "snippet_chars": snippet_chars,
        }

        self._run.call(node.value, args, sink=sink)
