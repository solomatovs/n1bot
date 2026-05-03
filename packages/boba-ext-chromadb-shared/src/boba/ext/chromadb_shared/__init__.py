"""Shared ChromaDB config: persist_path + embedding_model.

Делится между read-side (boba-ext-chromadb-tools) и write-side
(boba-ext-chromadb-store), чтобы оба consumer'а смотрели в одну базу
без дублирования настроек.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, OneOf, ParseString
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ChromadbSharedConfig", "ChromadbSharedSection"]


@dataclass(frozen=True)
class ChromadbSharedConfig:
    """Общие настройки ChromaDB-бэкенда."""

    persist_path: str = ""
    embedding_model: str = "default"


class ChromadbSharedSection(ConfigSection[ChromadbSharedConfig]):
    """Секция [ext.chromadb] — общие настройки tools и store."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[ChromadbSharedConfig]] = ObjectSchema(
        description=(
            "Общая конфигурация ChromaDB: путь к persistent store + "
            "embedding-модель. Делится между tools (read) и store (write)."
        ),
        fields=[
            FieldSpec(
                name="persist_path",
                coercer=ChainCoercer(Default(""), ParseString()),
                description=(
                    "Путь к persistent ChromaDB. Обязателен для обоих "
                    "consumer'ов (tools и store)."
                ),
            ),
            FieldSpec(
                name="embedding_model",
                coercer=ChainCoercer(
                    Default("default"), ParseString(), OneOf("default")
                ),
                description=(
                    "Модель эмбеддингов. v0.1: только 'default' (built-in ONNX). "
                    "Должна совпадать у tools и store."
                ),
            ),
        ],
        factory=ChromadbSharedConfig,
    )
