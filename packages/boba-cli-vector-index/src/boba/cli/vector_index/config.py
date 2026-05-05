"""ConfigSection'ы CLI-runner'а — per-handle DTO с своими required-полями."""

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
    "ACTIONS",
    "DeleteCommandConfig",
    "DeleteCommandSection",
    "IndexCommandConfig",
    "IndexCommandSection",
    "ShowCommandConfig",
    "ShowCommandSection",
    "SyncCommandConfig",
    "SyncCommandSection",
    "VectorIndexActionConfig",
    "VectorIndexActionSection",
    "VectorIndexChromadbConfig",
    "VectorIndexChromadbSection",
    "VectorIndexCommonConfig",
    "VectorIndexCommonSection",
    "command_section_for",
]


ACTIONS: frozenset[str] = frozenset(
    {"index", "list", "delete", "sync", "show"}
)


@dataclass(frozen=True)
class VectorIndexActionConfig:
    action: str


class VectorIndexActionSection(ConfigSection[VectorIndexActionConfig]):
    """Discriminator-секция: только `action`. Используется first-stage bootstrap'ом
    чтобы выбрать какую CommandSection регистрировать на second stage."""

    id: ClassVar[StrId] = StrId("vector_index_action")
    namespace: ClassVar[tuple[str, ...]] = ("vector_index",)

    schema: ClassVar[ObjectSchema[VectorIndexActionConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="action",
                coercer=ChainCoercer(ParseString(), OneOf(*sorted(ACTIONS))),
                required=True,
            ),
        ],
        factory=VectorIndexActionConfig,
    )


@dataclass(frozen=True)
class VectorIndexCommonConfig:
    verbose: int


class VectorIndexCommonSection(ConfigSection[VectorIndexCommonConfig]):
    """Общие поля runner'а (verbose). Регистрируется всегда."""

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
    pipeline: str
    description: str | None


class IndexCommandSection(ConfigSection[IndexCommandConfig]):
    """[vector_index.index] — параметры action=index."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index", "index")

    schema: ClassVar[ObjectSchema[IndexCommandConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="collection",
                coercer=ParseString(),
                required=True,
                description="Имя коллекции в Store.",
            ),
            FieldSpec(
                name="pipeline",
                coercer=ParseString(),
                required=True,
                description="PipelineId (entry-point boba.indexing.pipelines).",
            ),
            FieldSpec(
                name="description",
                coercer=Nullable(ParseString()),
                description="Описание коллекции (создаётся при первом index).",
            ),
        ],
        factory=IndexCommandConfig,
    )


@dataclass(frozen=True)
class SyncCommandConfig:
    collection: str
    pipeline: str


class SyncCommandSection(ConfigSection[SyncCommandConfig]):
    """[vector_index.sync] — параметры action=sync."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index", "sync")

    schema: ClassVar[ObjectSchema[SyncCommandConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="collection",
                coercer=ParseString(),
                required=True,
            ),
            FieldSpec(
                name="pipeline",
                coercer=ParseString(),
                required=True,
            ),
        ],
        factory=SyncCommandConfig,
    )


@dataclass(frozen=True)
class ShowCommandConfig:
    collection: str
    source_id: str
    limit: int
    snippet_chars: int


class ShowCommandSection(ConfigSection[ShowCommandConfig]):
    """[vector_index.show] — параметры action=show."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index", "show")

    schema: ClassVar[ObjectSchema[ShowCommandConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="collection",
                coercer=ParseString(),
                required=True,
            ),
            FieldSpec(
                name="source_id",
                coercer=ChainCoercer(Default(""), ParseString()),
                description="Фильтр по source_id; пусто — все чанки.",
            ),
            FieldSpec(
                name="limit",
                coercer=ChainCoercer(Default(20), ParseInt()),
            ),
            FieldSpec(
                name="snippet_chars",
                coercer=ChainCoercer(Default(200), ParseInt()),
            ),
        ],
        factory=ShowCommandConfig,
    )


@dataclass(frozen=True)
class DeleteCommandConfig:
    collection: str


class DeleteCommandSection(ConfigSection[DeleteCommandConfig]):
    """[vector_index.delete] — параметры action=delete."""

    namespace: ClassVar[tuple[str, ...]] = ("vector_index", "delete")

    schema: ClassVar[ObjectSchema[DeleteCommandConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="collection",
                coercer=ParseString(),
                required=True,
            ),
        ],
        factory=DeleteCommandConfig,
    )


@dataclass(frozen=True)
class VectorIndexChromadbConfig:
    persist_path: str


class VectorIndexChromadbSection(ConfigSection[VectorIndexChromadbConfig]):
    """[ext.chromadb] — `persist_path` обязателен для CLI.

    Явный `id` отличается от ChromadbSharedSection (та же namespace, разная DTO).
    """

    id: ClassVar[StrId] = StrId("vector_index_chromadb")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[VectorIndexChromadbConfig]] = ObjectSchema(
        fields=[
            FieldSpec(
                name="persist_path",
                coercer=ParseString(),
                required=True,
            ),
        ],
        factory=VectorIndexChromadbConfig,
    )


def command_section_for(action: str) -> ConfigSection | None:
    """ConfigSection для конкретного action; `None` для action без полей (list)."""
    mapping: dict[str, ConfigSection] = {
        "index": IndexCommandSection(),
        "sync": SyncCommandSection(),
        "show": ShowCommandSection(),
        "delete": DeleteCommandSection(),
    }
    return mapping.get(action)
