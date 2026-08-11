"""Описания узлов postgres для реестра стадий: контракт, entry, обогатители.

Профиль песочницы узла в описании не участвует — его подставляет сборка
реестра; здесь только знание инструмента о своих операциях.

Ошибки: UnknownConnectionError (boba.toolkit.sql) — имя подключения вне
whitelist'а; pydantic.ValidationError — args узла не по контракту.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from boba.tool.pg.executor import PgExecutorConfig
from boba.tool.pg.protocol import (
    PgCopyArgs,
    PgCopyRequest,
    PgCopyTrailer,
    PgQueryArgs,
    PgQueryRequest,
    PgStage,
)
from boba.toolkit.channels import StreamFormat
from boba.toolkit.sql import SqlQueryTrailer
from boba.toolkit.workflow import RequestArgs, StageContract, StageNode

__all__ = [
    "PgCopyEnricher",
    "PgCopyNode",
    "PgCopyOut",
    "PgQueryEnricher",
    "PgQueryNode",
    "PgStages",
]


class PgQueryEnricher:
    """Args узла pg_query -> запрос payload'а: профиль и потолок строк из конфига."""

    def __init__(self, cfg: PgExecutorConfig) -> None:
        self._cfg = cfg

    def __call__(self, args: Mapping[str, JsonValue]) -> Mapping[str, Any]:
        node_args = PgQueryArgs.model_validate(args)

        connection = self._cfg.resolve(node_args.connection_name)

        params: tuple[Any, ...] = tuple(node_args.params)

        request = PgQueryRequest(
            op=PgQueryRequest.OP,
            connection=connection,
            sql=node_args.sql,
            params=params,
            row_limit=self._cfg.max_rows,
        )

        return RequestArgs.of(request)


class PgCopyEnricher:
    """Args узла pg_copy -> запрос payload'а: профиль соединения из конфига."""

    def __init__(self, cfg: PgExecutorConfig) -> None:
        self._cfg = cfg

    def __call__(self, args: Mapping[str, JsonValue]) -> Mapping[str, Any]:
        node_args = PgCopyArgs.model_validate(args)

        connection = self._cfg.resolve(node_args.connection_name)

        request = PgCopyRequest(
            op=PgCopyRequest.OP,
            connection=connection,
            sql=node_args.sql,
            copy_format=node_args.copy_format,
        )

        return RequestArgs.of(request)


class PgCopyOut:
    """Формат продукта pg_copy — функция валидированного запроса узла."""

    def __call__(self, request: BaseModel) -> StreamFormat | None:
        if not isinstance(request, PgCopyRequest):
            msg = f"pg_copy out resolver got {type(request).__name__}"
            raise TypeError(msg)

        return request.copy_format.stream


class PgQueryNode:
    """Узел pg_query: строки NDJSON в канал данных, квитанция — SqlQueryTrailer."""

    NAME: ClassVar[str] = PgStage.QUERY
    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.pg.payload")
    REQUEST: ClassVar[type[BaseModel]] = PgQueryRequest
    CONTRACT: ClassVar[StageContract] = StageContract(
        accepts=frozenset(),
        out=StreamFormat.NDJSON,
        result=SqlQueryTrailer,
    )

    @staticmethod
    def enricher(cfg: PgExecutorConfig) -> PgQueryEnricher:
        return PgQueryEnricher(cfg)


class PgCopyNode:
    """Узел pg_copy: выгрузка COPY в канал данных, формат — аргумент узла."""

    NAME: ClassVar[str] = PgStage.COPY
    ENTRY: ClassVar[tuple[str, ...]] = PgQueryNode.ENTRY
    REQUEST: ClassVar[type[BaseModel]] = PgCopyRequest
    CONTRACT: ClassVar[StageContract] = StageContract(
        accepts=frozenset(),
        out=StreamFormat.TEXT,
        result=PgCopyTrailer,
    )
    """out — значение по умолчанию: реальный формат разрешает PgCopyOut."""

    @staticmethod
    def enricher(cfg: PgExecutorConfig) -> PgCopyEnricher:
        return PgCopyEnricher(cfg)

    @staticmethod
    def out_of() -> PgCopyOut:
        return PgCopyOut()


class PgStages:
    """Сборка узлов postgres для реестра стадий приложения."""

    @classmethod
    def of(cls, cfg: PgExecutorConfig) -> dict[str, StageNode]:
        nodes: dict[str, StageNode] = {}

        nodes[PgQueryNode.NAME] = StageNode(
            contract=PgQueryNode.CONTRACT,
            entry=PgQueryNode.ENTRY,
            request=PgQueryNode.REQUEST,
            enrich=PgQueryNode.enricher(cfg),
        )

        nodes[PgCopyNode.NAME] = StageNode(
            contract=PgCopyNode.CONTRACT,
            entry=PgCopyNode.ENTRY,
            request=PgCopyNode.REQUEST,
            enrich=PgCopyNode.enricher(cfg),
            out_of=PgCopyNode.out_of(),
        )

        return nodes
