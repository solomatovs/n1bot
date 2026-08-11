"""Узлы реестра стадий над HTML: контракт, entry, модели запросов, обогатитель."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from boba.tool.kb.html.protocol import (
    ConfluenceSectionsRequest,
    HtmlNode,
    HtmlToMarkdownRequest,
    PlainTextRequest,
)
from boba.toolkit.channels import StreamFormat
from boba.toolkit.workflow import EmptyTrailer, StageContract, StageNode

__all__ = ["HtmlNodeEnricher", "HtmlStages"]


class HtmlNodeEnricher:
    """args узла -> запрос payload'а: конфига у разбора разметки нет, только op."""

    def __init__(self, node: HtmlNode) -> None:
        self._node = node

    def __call__(self, args: Mapping[str, JsonValue], /) -> Mapping[str, Any]:
        enriched: dict[str, Any] = dict(args)
        enriched["op"] = self._node

        return enriched


class HtmlStages:
    """Сборка узлов разбора HTML для реестра стадий приложения."""

    ENTRY: ClassVar[tuple[str, ...]] = (
        "python3",
        "-m",
        "boba.tool.kb.html.payload",
    )

    ACCEPTS: ClassVar[frozenset[StreamFormat]] = frozenset({StreamFormat.TEXT})
    """Вход узла — разметка страницы: текстовый поток."""

    REQUESTS: ClassVar[Mapping[HtmlNode, type[BaseModel]]] = {
        HtmlNode.MARKDOWN: HtmlToMarkdownRequest,
        HtmlNode.PLAIN_TEXT: PlainTextRequest,
        HtmlNode.SECTIONS: ConfluenceSectionsRequest,
    }

    OUT: ClassVar[Mapping[HtmlNode, StreamFormat]] = {
        HtmlNode.MARKDOWN: StreamFormat.TEXT,
        HtmlNode.PLAIN_TEXT: StreamFormat.TEXT,
        HtmlNode.SECTIONS: StreamFormat.NDJSON,
    }

    @classmethod
    def of(cls) -> dict[str, StageNode]:
        builds: dict[str, StageNode] = {}

        for node, request in cls.REQUESTS.items():
            contract = StageContract(
                accepts=cls.ACCEPTS,
                out=cls.OUT[node],
                result=EmptyTrailer,
            )
            builds[node.value] = StageNode(
                contract=contract,
                entry=cls.ENTRY,
                request=request,
                enrich=HtmlNodeEnricher(node),
            )

        return builds
