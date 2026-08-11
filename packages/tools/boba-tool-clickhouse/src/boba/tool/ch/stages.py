"""Описания узлов clickhouse для реестра стадий: контракт, entry, обогатители.

Профиль песочницы узла в описании не участвует — его подставляет сборка
реестра; здесь только знание инструмента о своих операциях.

Ошибки: ValueError — имя подключения вне whitelist'а; pydantic.ValidationError —
args узла не по контракту.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from boba.tool.ch.executor import ChExecutorConfig
from boba.tool.ch.protocol import (
    ChInsertArgs,
    ChInsertRequest,
    ChInsertTrailer,
    ChQueryArgs,
    ChQueryRequest,
    ChQueryTrailer,
    ChStage,
)
from boba.toolkit.channels import StreamFormat
from boba.toolkit.workflow import RequestArgs, StageContract, StageNode

__all__ = [
    "ChInsertEnricher",
    "ChInsertNode",
    "ChQueryEnricher",
    "ChQueryNode",
    "ChStages",
]


class ChQueryEnricher:
    """Args узла ch_query -> запрос payload'а: read-only профиль и потолок строк."""

    def __init__(self, cfg: ChExecutorConfig) -> None:
        self._cfg = cfg

    def __call__(self, args: Mapping[str, JsonValue]) -> Mapping[str, Any]:
        node_args = ChQueryArgs.model_validate(args)

        connection = self._cfg.resolve(node_args.connection_name).read_only()

        request = ChQueryRequest(
            op=ChQueryRequest.OP,
            connection=connection,
            sql=node_args.sql,
            params=dict(node_args.params),
            row_limit=self._cfg.max_rows,
        )

        return RequestArgs.of(request)


class ChInsertEnricher:
    """Args узла ch_insert -> запрос payload'а: профиль пишущей сессии из конфига."""

    def __init__(self, cfg: ChExecutorConfig) -> None:
        self._cfg = cfg

    def __call__(self, args: Mapping[str, JsonValue]) -> Mapping[str, Any]:
        node_args = ChInsertArgs.model_validate(args)

        connection = self._cfg.resolve(node_args.connection_name)

        request = ChInsertRequest(
            op=ChInsertRequest.OP,
            connection=connection,
            table=node_args.table,
            stdin_format=node_args.stdin_format,
        )

        return RequestArgs.of(request)


class ChQueryNode:
    """Узел ch_query: строки NDJSON в канал данных, квитанция — ChQueryTrailer."""

    NAME: ClassVar[str] = ChStage.QUERY
    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.ch.payload")
    REQUEST: ClassVar[type[BaseModel]] = ChQueryRequest
    CONTRACT: ClassVar[StageContract] = StageContract(
        accepts=frozenset(),
        out=StreamFormat.NDJSON,
        result=ChQueryTrailer,
    )

    @staticmethod
    def enricher(cfg: ChExecutorConfig) -> ChQueryEnricher:
        return ChQueryEnricher(cfg)


class ChInsertNode:
    """Узел ch_insert: поток со stdin уходит в таблицу, наружу — только квитанция."""

    NAME: ClassVar[str] = ChStage.INSERT
    ENTRY: ClassVar[tuple[str, ...]] = ChQueryNode.ENTRY
    REQUEST: ClassVar[type[BaseModel]] = ChInsertRequest
    CONTRACT: ClassVar[StageContract] = StageContract(
        accepts=ChInsertRequest.ACCEPTS,
        out=None,
        result=ChInsertTrailer,
    )

    @staticmethod
    def enricher(cfg: ChExecutorConfig) -> ChInsertEnricher:
        return ChInsertEnricher(cfg)


class ChStages:
    """Сборка узлов clickhouse для реестра стадий приложения."""

    @classmethod
    def of(cls, cfg: ChExecutorConfig) -> dict[str, StageNode]:
        nodes: dict[str, StageNode] = {}

        nodes[ChQueryNode.NAME] = StageNode(
            contract=ChQueryNode.CONTRACT,
            entry=ChQueryNode.ENTRY,
            request=ChQueryNode.REQUEST,
            enrich=ChQueryNode.enricher(cfg),
        )

        nodes[ChInsertNode.NAME] = StageNode(
            contract=ChInsertNode.CONTRACT,
            entry=ChInsertNode.ENTRY,
            request=ChInsertNode.REQUEST,
            enrich=ChInsertNode.enricher(cfg),
        )

        return nodes
