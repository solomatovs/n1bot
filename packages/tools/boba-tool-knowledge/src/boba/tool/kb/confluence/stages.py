"""Узлы реестра стадий чтения Confluence: контракты, entry, запросы, обогатители."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import JsonValue

from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.protocol import (
    ConfluenceGrepRequest,
    ConfluenceNode,
    ConfluencePageRequest,
    ConfluencePageTrailer,
    ConfluenceSearchRequest,
    ConfluenceSpacesRequest,
)
from boba.tool.kb.confluence.tools_config import ConfluenceToolsConfig
from boba.toolkit.channels import StreamFormat
from boba.toolkit.workflow import EmptyTrailer, StageContract, StageNode

__all__ = ["ConfluenceReadEnricher", "ConfluenceStages"]


class ConfluenceReadEnricher:
    """args операции чтения -> запрос payload'а: адрес, профиль и лимиты конфига."""

    def __init__(
        self,
        node: ConfluenceNode,
        connection: ConfluenceConnection,
        settings: Mapping[str, JsonValue],
    ) -> None:
        self._node = node
        self._connection = connection
        self._settings = dict(settings)

    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        enriched: dict[str, Any] = dict(args)
        enriched.update(self._settings)
        enriched["op"] = self._node
        enriched["base_url"] = self._connection.base_url
        enriched["profile"] = self._connection.profile

        return enriched


class ConfluenceStages:
    """Сборка узлов чтения Confluence для реестра стадий приложения."""

    ENTRY: ClassVar[tuple[str, ...]] = (
        "python3",
        "-m",
        "boba.tool.kb.confluence.payload",
    )

    @classmethod
    def of(cls, cfg: ConfluenceToolsConfig) -> dict[str, StageNode]:
        connection = cfg.connection()

        page_settings: dict[str, JsonValue] = {"body_format": cfg.body_format}
        grep_settings: dict[str, JsonValue] = {
            "body_format": cfg.body_format,
            "max_text_chars": cfg.max_text_chars,
        }
        spaces_settings: dict[str, JsonValue] = {"limit": cfg.page_size}

        nodes: dict[str, StageNode] = {}

        nodes[ConfluenceNode.PAGE.value] = StageNode(
            contract=StageContract(
                out=StreamFormat.TEXT,
                result=ConfluencePageTrailer,
            ),
            entry=cls.ENTRY,
            request=ConfluencePageRequest,
            enrich=ConfluenceReadEnricher(
                ConfluenceNode.PAGE, connection, page_settings
            ),
        )

        nodes[ConfluenceNode.GREP.value] = StageNode(
            contract=cls._rows_contract(),
            entry=cls.ENTRY,
            request=ConfluenceGrepRequest,
            enrich=ConfluenceReadEnricher(
                ConfluenceNode.GREP, connection, grep_settings
            ),
        )

        nodes[ConfluenceNode.SEARCH.value] = StageNode(
            contract=cls._rows_contract(),
            entry=cls.ENTRY,
            request=ConfluenceSearchRequest,
            enrich=ConfluenceReadEnricher(ConfluenceNode.SEARCH, connection, {}),
        )

        nodes[ConfluenceNode.SPACES.value] = StageNode(
            contract=cls._rows_contract(),
            entry=cls.ENTRY,
            request=ConfluenceSpacesRequest,
            enrich=ConfluenceReadEnricher(
                ConfluenceNode.SPACES, connection, spaces_settings
            ),
        )

        return nodes

    @staticmethod
    def _rows_contract() -> StageContract:
        return StageContract(
            out=StreamFormat.NDJSON,
            result=EmptyTrailer,
        )
