"""Узлы реестра стадий индексации Confluence и чтения вложений."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import JsonValue

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_protocol import (
    ConfluenceIngestRequest,
    IngestAnswer,
)
from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceNode,
)
from boba.tool.kb.confluence.stages import ConfluenceStages
from boba.tool.kb.confluence.tools_config import ConfluenceAttachmentSettings
from boba.toolkit.channels import StreamFormat
from boba.toolkit.workflow import EmptyTrailer, StageContract, StageNode

__all__ = [
    "ConfluenceAttachmentEnricher",
    "ConfluenceIngestEnricher",
    "ConfluenceIngestStages",
]


class ConfluenceAttachmentEnricher:
    """args вложения -> запрос payload'а: адрес, профиль и настройки парсера."""

    def __init__(
        self,
        connection: ConfluenceConnection,
        parser: ConfluenceAttachmentSettings,
    ) -> None:
        self._connection = connection
        self._parser = parser

    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        enriched: dict[str, Any] = dict(args)
        enriched["op"] = ConfluenceNode.ATTACHMENT
        enriched["base_url"] = self._connection.base_url
        enriched["profile"] = self._connection.profile
        enriched["body_format"] = self._connection.body_format
        enriched["max_pages"] = self._parser.max_pages
        enriched["tessdata_path"] = self._parser.tessdata_path

        return enriched


class ConfluenceIngestEnricher:
    """args прогона -> запрос payload'а: конфиг индексации целиком плюс op."""

    def __init__(self, cfg: ConfluenceIngestConfig) -> None:
        self._cfg = cfg

    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        enriched: dict[str, Any] = {}
        for name, value in args.items():
            if name in ConfluenceIngestStages.PARSER_ARGS:
                continue
            enriched[name] = value

        enriched["op"] = ConfluenceNode.INGEST
        enriched["config"] = self._config(args)

        return enriched

    def _config(self, args: Mapping[str, JsonValue]) -> ConfluenceIngestConfig:
        """Настройки парсера из вызова поверх секции конфига."""
        update: dict[str, JsonValue] = {}
        for name in ConfluenceIngestStages.PARSER_ARGS:
            if name in args:
                update[name] = args[name]

        if not update:
            return self._cfg

        return self._cfg.model_copy(update=update)


class ConfluenceIngestStages:
    """Сборка узлов индексации и чтения вложений для реестра стадий."""

    INGEST_ENTRY: ClassVar[tuple[str, ...]] = (
        "python3",
        "-m",
        "boba.tool.kb.confluence.ingest_payload",
    )

    PARSER_ARGS: ClassVar[tuple[str, ...]] = (
        "ocr_enabled",
        "num_workers",
        "ocr_language",
    )
    """Настройки парсера, которые вызывающий вправе задать поверх конфига."""

    @classmethod
    def of(cls, cfg: ConfluenceIngestConfig) -> dict[str, StageNode]:
        connection = ConfluenceConnection(
            profile=cfg.confluence, body_format=cfg.body_format
        )
        parser = ConfluenceAttachmentSettings(
            max_pages=cfg.max_pages,
            tessdata_path=cfg.tessdata_path,
        )

        nodes: dict[str, StageNode] = {}

        nodes[ConfluenceNode.INGEST.value] = StageNode(
            contract=StageContract(
                accepts=frozenset(),
                out=None,
                result=IngestAnswer,
            ),
            entry=cls.INGEST_ENTRY,
            request=ConfluenceIngestRequest,
            enrich=ConfluenceIngestEnricher(cfg),
        )

        nodes[ConfluenceNode.ATTACHMENT.value] = StageNode(
            contract=StageContract(
                accepts=frozenset(),
                out=StreamFormat.TEXT,
                result=EmptyTrailer,
            ),
            entry=ConfluenceStages.ENTRY,
            request=ConfluenceAttachmentRequest,
            enrich=ConfluenceAttachmentEnricher(connection, parser),
        )

        return nodes
