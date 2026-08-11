"""Вызов web-узлов: скачивание и поиск идут внутри песочницы.

Каждый вызов — вырожденный граф из одного узла: аргументы едут в спеке,
профиль соединения добавляет обогатитель узла, продукт стадии читает sink
вызывающего, а квитанция достаётся из итога графа.
"""

from __future__ import annotations

from boba.toolkit.channels import ChannelSink
from boba.toolkit.launcher import LauncherFactory, StageRun
from boba.web.protocol import (
    WebFetchArgs,
    WebFetchTrailer,
    WebGrepArgs,
    WebGrepTrailer,
    WebOp,
)

__all__ = ["WebCaller"]


class WebCaller:
    """Один вызов payload'а на запрос; узел выбирается операцией."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def fetch(self, args: WebFetchArgs, sink: ChannelSink) -> WebFetchTrailer:
        return self._run.trailer(
            WebOp.FETCH.value,
            args.model_dump(mode="json"),
            WebFetchTrailer,
            sink=sink,
        )

    def grep(self, args: WebGrepArgs, sink: ChannelSink) -> WebGrepTrailer:
        return self._run.trailer(
            WebOp.GREP.value,
            args.model_dump(mode="json"),
            WebGrepTrailer,
            sink=sink,
        )
