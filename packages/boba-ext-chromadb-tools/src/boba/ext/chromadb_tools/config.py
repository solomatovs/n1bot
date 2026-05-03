"""Секция [ext.chromadb.tools]: гейт + per-tools параметры (snippet_chars)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    ParseBool,
    ParseCsvList,
    ParseInt,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ChromadbToolsConfig", "ChromadbToolsSection"]


@dataclass(frozen=True)
class ChromadbToolsConfig:
    enable: bool = False
    tools_allow: list[str] = field(default_factory=list)
    snippet_chars: int = 300


class ChromadbToolsSection(ConfigSection[ChromadbToolsConfig]):
    """Секция [ext.chromadb.tools] — гейт и общие настройки read-tools."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb", "tools")

    schema: ClassVar[ObjectSchema[ChromadbToolsConfig]] = ObjectSchema(
        description=(
            "Read-only ChromaDB-tools (kb_search, kb_list_collections). "
            "Включаются явно через enable=true; persist_path берётся из общей "
            "[ext.chromadb] секции (boba-ext-chromadb-shared)."
        ),
        fields=[
            FieldSpec(
                name="enable",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Подключить ChromaDB read-tools.",
            ),
            FieldSpec(
                name="tools_allow",
                coercer=ParseCsvList(),
                description=(
                    "Whitelist по именам tools (kb_search, kb_list_collections). "
                    "Пусто — регистрируются все."
                ),
            ),
            FieldSpec(
                name="snippet_chars",
                coercer=ChainCoercer(Default(300), ParseInt(), MinValue(1)),
                description="Максимальная длина сниппета документа в kb_search.",
            ),
        ],
        factory=ChromadbToolsConfig,
    )
