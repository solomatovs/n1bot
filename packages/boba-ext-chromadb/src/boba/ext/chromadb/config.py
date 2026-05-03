"""Конфигурация ChromaDB-extension."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    OneOf,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ChromaExtConfig", "ChromadbSection"]


@dataclass(frozen=True)
class ChromaExtConfig:
    enable: bool = False
    tools_allow: list[str] = field(default_factory=list)
    persist_path: str = ""
    embedding_model: str = "default"
    snippet_chars: int = 300


class ChromadbSection(ConfigSection[ChromaExtConfig]):
    """Секция конфига расширения chromadb."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[ChromaExtConfig]] = ObjectSchema(
        description=(
            "Конфигурация ChromaDB-extension: enable + путь к persistent store, "
            "embedding-модель, лимиты kb_search. "
            "Включается явно через enable=true; при включении persist_path обязателен."
        ),
        fields=[
            FieldSpec(
                name="enable",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Подключить ChromaDB kb_search/kb_list_collections.",
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
                name="persist_path",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "Путь к persistent ChromaDB (общий с boba-cli-vector-index, "
                    "чтобы агент видел свежепроиндексированные коллекции). "
                    "Обязателен при enable=true."
                ),
            ),
            FieldSpec(
                name="embedding_model",
                coercer=ChainCoercer(
                    Default("default"),
                    ParseString(),
                    OneOf("default"),
                ),
                description=(
                    "Модель эмбеддингов. v0.1: только 'default' (built-in ONNX). "
                    "Расширим, когда добавим sentence-transformers как optional dep."
                ),
            ),
            FieldSpec(
                name="snippet_chars",
                coercer=ChainCoercer(Default(300), ParseInt(), MinValue(1)),
                description="Максимальная длина сниппета документа "
                "в результате kb_search.",
            ),
        ],
        factory=ChromaExtConfig,
    )
