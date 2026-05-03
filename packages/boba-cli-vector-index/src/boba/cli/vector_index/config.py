"""Конфиг boba-cli-vector-index как ConfigSection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    Nullable,
    OneOf,
    ParseBool,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = ["ACTIONS", "VectorIndexConfig", "VectorIndexSection"]


ACTIONS: frozenset[str] = frozenset({"index", "list", "delete"})


@dataclass(frozen=True)
class VectorIndexConfig:
    """Параметры одного запуска boba-cli-vector-index."""

    action: str
    collection: str | None
    description: str | None
    confirm_skip: bool
    verbose: int
    source: str
    chunker: str
    store: str


class VectorIndexSection(ConfigSection[VectorIndexConfig]):
    """Секция vector_index — runner CLI-индексатора векторной базы."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index",)

    schema: ClassVar[ObjectSchema[VectorIndexConfig]] = ObjectSchema(
        description=(
            "Параметры одного запуска CLI-индексатора: action + collection + "
            "id плагинов (source/chunker/store). Параметры самих плагинов — в "
            "их собственных конфиг-секциях."
        ),
        fields=[
            FieldSpec(
                name="action",
                coercer=ChainCoercer(ParseString(), OneOf(*sorted(ACTIONS))),
                required=True,
                description=f"Что делать: один из {sorted(ACTIONS)}. Обязательно.",
            ),
            FieldSpec(
                name="collection",
                coercer=Nullable(ParseString()),
                description=(
                    "Имя коллекции в Store. Обязательно для action=index/delete."
                ),
            ),
            FieldSpec(
                name="description",
                coercer=Nullable(ParseString()),
                description=(
                    "Описание коллекции; применяется только при создании."
                ),
            ),
            FieldSpec(
                name="confirm_skip",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description="Пропустить интерактивное подтверждение (action=delete).",
            ),
            FieldSpec(
                name="verbose",
                coercer=ChainCoercer(Default(0), ParseInt()),
                description="Verbosity logging: 0=WARN, 1=INFO, 2=DEBUG.",
            ),
            FieldSpec(
                name="source",
                coercer=ChainCoercer(Default("ext.fs"), ParseString()),
                description=(
                    "SourceId плагина (entry-point boba.indexing.sources). "
                    "Default 'ext.fs'."
                ),
            ),
            FieldSpec(
                name="chunker",
                coercer=ChainCoercer(Default("ext.sliding"), ParseString()),
                description=(
                    "ChunkerId плагина (entry-point boba.indexing.chunkers). "
                    "Default 'ext.sliding'."
                ),
            ),
            FieldSpec(
                name="store",
                coercer=ChainCoercer(Default("ext.chromadb_persist"), ParseString()),
                description=(
                    "StoreId плагина (entry-point boba.indexing.stores). "
                    "Default 'ext.chromadb_persist'."
                ),
            ),
        ],
        factory=VectorIndexConfig,
    )
