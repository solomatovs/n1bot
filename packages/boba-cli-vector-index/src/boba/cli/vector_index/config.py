"""ConfigSection'ы CLI: action+pipeline discriminator + index command DTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    Nullable,
    OneOf,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId

__all__ = [
    "ALL_ACTIONS",
    "IndexCommandConfig",
    "IndexCommandSection",
    "PrintCommandConfig",
    "PrintCommandSection",
    "VectorIndexActionConfig",
    "VectorIndexActionSection",
    "VectorIndexChromadbConfig",
    "VectorIndexChromadbSection",
    "VectorIndexCommonConfig",
    "VectorIndexCommonSection",
]


ALL_ACTIONS: frozenset[str] = frozenset({"index", "print"})


@dataclass(frozen=True)
class VectorIndexActionConfig:
    action: str
    pipeline: str | None


class VectorIndexActionSection(ConfigSection[VectorIndexActionConfig]):
    """`[vector_index]` action+pipeline discriminator (pipeline только для index)."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index",)

    schema: ClassVar[ObjectSchema[VectorIndexActionConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="action",
                coercer=ChainCoercer(
                    ParseString(),
                    OneOf(*sorted(ALL_ACTIONS)),
                ),
                required=True,
            ),
            FieldSpec(
                name="pipeline",
                coercer=Nullable(ParseString()),
                description="ID pipeline-плагина (обязателен для action=index).",
            ),
        ],
        factory=VectorIndexActionConfig,
    )


@dataclass(frozen=True)
class VectorIndexCommonConfig:
    verbose: int


class VectorIndexCommonSection(ConfigSection[VectorIndexCommonConfig]):
    """`[vector_index]` verbose."""

    id: ClassVar[StrId] = StrId("vector_index_common")
    namespace: ClassVar[tuple[str, ...]] = ("vector_index",)

    schema: ClassVar[ObjectSchema[VectorIndexCommonConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="verbose",
                coercer=ChainCoercer(Default(0), ParseInt()),
                description="0=WARN, 1=INFO, 2=DEBUG.",
            ),
        ],
        factory=VectorIndexCommonConfig,
    )


@dataclass(frozen=True)
class IndexCommandConfig:
    collection: str
    description: str | None


class IndexCommandSection(ConfigSection[IndexCommandConfig]):
    """`[vector_index.index]` — параметры action=index."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index", "index")

    schema: ClassVar[ObjectSchema[IndexCommandConfig]] = ObjectSchema(
        fields=[
            FieldSpec(name="collection", coercer=ParseString(), required=True),
            FieldSpec(
                name="description",
                coercer=Nullable(ParseString()),
                description="Описание коллекции (создаётся при первом index).",
            ),
        ],
        factory=IndexCommandConfig,
    )


@dataclass(frozen=True)
class PrintCommandConfig:
    collection: str
    source_id: str | None
    limit: int
    snippet_chars: int


class PrintCommandSection(ConfigSection[PrintCommandConfig]):
    """`[vector_index.print]` — параметры action=print."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index", "print")

    schema: ClassVar[ObjectSchema[PrintCommandConfig]] = ObjectSchema(
        fields=[
            FieldSpec(name="collection", coercer=ParseString(), required=True),
            FieldSpec(
                name="source_id",
                coercer=Nullable(ParseString()),
                description="Фильтр по конкретному source_id; пусто = все чанки.",
            ),
            FieldSpec(
                name="limit",
                coercer=ChainCoercer(Default(20), ParseInt(), MinValue(1)),
                description="Сколько чанков показать максимум.",
            ),
            FieldSpec(
                name="snippet_chars",
                coercer=ChainCoercer(Default(200), ParseInt(), MinValue(1)),
                description="Длина preview-строки чанка в символах.",
            ),
        ],
        factory=PrintCommandConfig,
    )


@dataclass(frozen=True)
class VectorIndexChromadbConfig:
    persist_path: str


class VectorIndexChromadbSection(ConfigSection[VectorIndexChromadbConfig]):
    """`[ext.chromadb]` — `persist_path` обязателен для CLI."""

    id: ClassVar[StrId] = StrId("vector_index_chromadb")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[VectorIndexChromadbConfig]] = ObjectSchema(
        fields=[
            FieldSpec(name="persist_path", coercer=ParseString(), required=True),
        ],
        factory=VectorIndexChromadbConfig,
    )
