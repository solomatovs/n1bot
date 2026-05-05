"""ConfigSection'ы CLI: action+pipeline discriminator + index command DTO."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
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
    "VectorIndexActionConfig",
    "VectorIndexActionSection",
    "VectorIndexChromadbConfig",
    "VectorIndexChromadbSection",
    "VectorIndexCommonConfig",
    "VectorIndexCommonSection",
]


ALL_ACTIONS: frozenset[str] = frozenset({"index"})


@dataclass(frozen=True)
class VectorIndexActionConfig:
    action: str
    pipeline: str


class VectorIndexActionSection(ConfigSection[VectorIndexActionConfig]):
    """`[vector_index]` action+pipeline discriminator."""

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
                coercer=ParseString(),
                required=True,
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
