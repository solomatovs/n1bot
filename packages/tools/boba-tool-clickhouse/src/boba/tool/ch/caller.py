"""Вызов clickhouse-узлов: одиночный вызов — вырожденный граф из одного узла.

Профиль соединения в спецификацию не попадает: узел получает имя подключения,
а профиль с секретами подставляет обогатитель реестра стадий.

Ошибки:
LauncherError — исполнитель нарушил контракт.
PayloadFailureError — payload объявил ожидаемый отказ.
WorkflowError — контракт узла нарушен.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from boba.tool.ch.protocol import ChQueryArgs, ChQueryTrailer, ChStage
from boba.toolkit.channels import ChannelSink
from boba.toolkit.launcher import LauncherFactory, StageRun

__all__ = ["ChCaller"]


class ChCaller:
    """Один вызов payload'а на запрос; клиент не переживает вызов по построению."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def query(
        self,
        *,
        connection_name: str,
        sql: str,
        params: Mapping[str, Any],
        sink: ChannelSink,
    ) -> ChQueryTrailer:
        args = ChQueryArgs(
            connection_name=connection_name,
            sql=sql,
            params=dict(params),
        )

        return self._run.trailer(
            ChStage.QUERY.value,
            args.model_dump(mode="json"),
            ChQueryTrailer,
            sink=sink,
        )
