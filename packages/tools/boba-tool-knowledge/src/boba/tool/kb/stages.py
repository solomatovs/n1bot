"""Узлы реестра стадий kb-поиска: контракт, entry, модель запроса, обогатитель."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import JsonValue

from boba.tool.kb.kb import PostgresKnowledgeBaseConfig
from boba.tool.kb.protocol import EmbeddingRef, KbNode, KbSearchRequest
from boba.toolkit.channels import StreamFormat
from boba.toolkit.workflow import EmptyTrailer, StageContract, StageNode

__all__ = ["KbSearchEnricher", "KbStages"]


class KbSearchEnricher:
    """args поиска -> запрос payload'а: соединение, таблицы и эмбеддер из конфига."""

    def __init__(self, cfg: PostgresKnowledgeBaseConfig) -> None:
        self._cfg = cfg

    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        embedding = EmbeddingRef(
            model=self._cfg.embedding.model,
            cache_dir=self._cfg.embedding.cache_dir,
        )

        enriched: dict[str, Any] = dict(args)
        enriched["op"] = KbNode.SEARCH
        enriched["connection"] = self._cfg.connection
        enriched["schema_name"] = self._cfg.tables.pg_schema
        enriched["chunks_table"] = self._cfg.tables.chunks_table
        enriched["embedding"] = embedding

        return enriched


class KbStages:
    """Сборка узлов kb для реестра стадий приложения."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.kb.payload")

    CONTRACT: ClassVar[StageContract] = StageContract(
        out=StreamFormat.NDJSON,
        result=EmptyTrailer,
    )
    """Строки выдачи — NDJSON, квитанция пуста."""

    @classmethod
    def of(cls, cfg: PostgresKnowledgeBaseConfig) -> dict[str, StageNode]:
        search = StageNode(
            contract=cls.CONTRACT,
            entry=cls.ENTRY,
            request=KbSearchRequest,
            enrich=KbSearchEnricher(cfg),
        )

        return {KbNode.SEARCH.value: search}
