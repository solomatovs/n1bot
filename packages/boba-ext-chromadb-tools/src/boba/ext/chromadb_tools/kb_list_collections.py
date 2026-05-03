"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.chromadb_tools.kb import ChromaKnowledgeBase
from boba.tools import (
    ParamOverlay,
    Tool,
    ToolContext,
    ToolId,
    ToolResult,
    ToolSourceId,
    params_field,
)


@dataclass(frozen=True)
class KbListCollectionsArgs:
    """Без параметров — kb_list_collections аргументов не принимает."""


@dataclass(frozen=True)
class KbListCollectionsToolConfig:
    """DTO секции [ext.chromadb.tools.kb_list_collections]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class KbListCollectionsTool(Tool[KbListCollectionsArgs]):
    """Возвращает JSON [{name, description}] доступных коллекций."""

    _ID = ToolId("kb_list_collections")
    _SOURCE = ToolSourceId("ext.chromadb_tools")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Список доступных knowledge-base коллекций ChromaDB. "
        "Возвращает JSON-массив объектов "
        "{name, description}. Используй перед kb_search чтобы "
        "выбрать подходящую коллекцию."
    )

    def __init__(
        self,
        kb: ChromaKnowledgeBase,
        cfg: KbListCollectionsToolConfig,
    ) -> None:
        self._kb = kb
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[KbListCollectionsArgs]:
        return ObjectSchema(
            description=self._cfg.description,
            fields=[],
            factory=KbListCollectionsArgs,
        )

    def execute(self, ctx: ToolContext, req: KbListCollectionsArgs) -> ToolResult:
        del ctx, req
        items = [
            {"name": c.name, "description": c.description}
            for c in self._kb.list_collections()
        ]
        return ToolResult(content=json.dumps(items, ensure_ascii=False))


class KbListCollectionsToolSection(ConfigSection[KbListCollectionsToolConfig]):
    """Секция [ext.chromadb.tools.kb_list_collections]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext",
        "chromadb",
        "tools",
        "kb_list_collections",
    )

    schema: ClassVar[ObjectSchema[KbListCollectionsToolConfig]] = ObjectSchema(
        description="Конфиг tool 'kb_list_collections'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(KbListCollectionsTool.DEFAULT_DESCRIPTION),
                    ParseString(),
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=KbListCollectionsToolConfig,
    )
