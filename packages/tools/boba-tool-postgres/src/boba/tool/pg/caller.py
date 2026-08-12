"""Вызов postgres-узлов: одиночный вызов — вырожденный граф из одного узла.

Профиль соединения в спецификацию не попадает: узел получает имя подключения,
а профиль с секретами подставляет обогатитель реестра стадий.

Ошибки: LauncherError — исполнитель нарушил контракт; PayloadFailureError —
payload объявил ожидаемый отказ (СУБД недоступна, запрос отклонён);
WorkflowError — спецификация или контракт узла нарушены.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from boba.tool.pg.protocol import (
    PgCopyArgs,
    PgCopyDirection,
    PgCopyFormat,
    PgCopyTrailer,
    PgQueryArgs,
    PgStage,
)
from boba.toolkit.channels import ChannelSink
from boba.toolkit.launcher import LauncherFactory, StageRun
from boba.toolkit.sql import SqlQueryTrailer

__all__ = ["PgCaller"]

M = TypeVar("M", bound=BaseModel)


class PgCaller:
    """Один вызов payload'а на запрос; пул не переживает вызов по построению."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def query(
        self,
        *,
        connection_name: str,
        sql: str,
        params: Sequence[Any],
        sink: ChannelSink,
    ) -> SqlQueryTrailer:
        args = PgQueryArgs(
            connection_name=connection_name,
            sql=sql,
            params=list(params),
        )

        return self._stage(PgStage.QUERY, args, sink, SqlQueryTrailer)

    def copy(
        self,
        *,
        connection_name: str,
        sql: str,
        copy_format: PgCopyFormat,
        sink: ChannelSink,
    ) -> PgCopyTrailer:
        """Выгрузка фасада: запрос вызывающего оборачивается в COPY ... TO STDOUT."""
        args = PgCopyArgs(
            connection_name=connection_name,
            direction=PgCopyDirection.TO_STDOUT,
            sql=copy_format.statement(sql),
        )

        return self._stage(PgStage.COPY, args, sink, PgCopyTrailer)

    def _stage(
        self,
        stage: PgStage,
        args: BaseModel,
        sink: ChannelSink,
        trailer: type[M],
    ) -> M:
        return self._run.trailer(
            stage.value,
            args.model_dump(mode="json"),
            trailer,
            sink=sink,
        )
