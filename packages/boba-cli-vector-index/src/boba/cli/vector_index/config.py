"""Конфиг boba-cli-vector-index как ConfigSection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.cli.vector_index.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.coercion import (
    ChainCoercer,
    Default,
    Nullable,
    OneOf,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

__all__ = [
    "ACTIONS",
    "ChromadbPersistConfig",
    "ChromadbPersistSection",
    "VectorIndexConfig",
    "VectorIndexSection",
]


ACTIONS: frozenset[str] = frozenset({"index", "list", "delete"})


@dataclass(frozen=True)
class VectorIndexConfig:
    """Параметры одного запуска boba-cli-vector-index."""

    action: str
    paths: list[str] | None
    collection: str | None
    description: str | None
    chunk_size: int
    chunk_overlap: int
    confirm_skip: bool
    verbose: int


class VectorIndexSection(ConfigSection[VectorIndexConfig]):
    """Секция vector_index."""

    id: ClassVar[StrId] = StrId("vector_index")
    namespace: ClassVar[tuple[str, ...]] = ("vector_index",)

    schema: ClassVar[ObjectSchema[VectorIndexConfig]] = ObjectSchema(
        description=(
            "Параметры одного запуска CLI-индексатора векторной базы: "
            "action + per-action входы (paths/collection/...) + знобы "
            "чанкирования."
        ),
        fields=[
            FieldSpec(
                name="action",
                coercer=ChainCoercer(
                    ParseString(),
                    OneOf(*sorted(ACTIONS)),
                ),
                required=True,
                description=(f"Что делать: один из {sorted(ACTIONS)}. Обязательно."),
            ),
            FieldSpec(
                name="paths",
                coercer=Nullable(ParseCsvList()),
                description=(
                    "Файлы/директории для индексации (CSV в env, "
                    "TOML-array). Обязательно для action=index."
                ),
            ),
            FieldSpec(
                name="collection",
                coercer=Nullable(ParseString()),
                description=(
                    "Имя коллекции в ChromaDB. Обязательно для action=index/delete."
                ),
            ),
            FieldSpec(
                name="description",
                coercer=Nullable(ParseString()),
                description=(
                    "Описание коллекции (видно агенту через "
                    "kb_list_collections). Применяется только при "
                    "создании коллекции."
                ),
            ),
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(
                    Default(DEFAULT_CHUNK_SIZE),
                    ParseInt(),
                ),
                description=(
                    f"Размер чанка в символах (default {DEFAULT_CHUNK_SIZE})."
                ),
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(
                    Default(DEFAULT_CHUNK_OVERLAP),
                    ParseInt(),
                ),
                description=(
                    f"Перекрытие чанков в символах (default {DEFAULT_CHUNK_OVERLAP})."
                ),
            ),
            FieldSpec(
                name="confirm_skip",
                coercer=ChainCoercer(Default(False), ParseBool()),
                description=("Пропустить интерактивное подтверждение (action=delete)."),
            ),
            FieldSpec(
                name="verbose",
                coercer=ChainCoercer(Default(0), ParseInt()),
                description=("Verbosity logging: 0=WARN, 1=INFO, 2=DEBUG."),
            ),
        ],
        factory=VectorIndexConfig,
    )


@dataclass(frozen=True)
class ChromadbPersistConfig:
    """Минимальный DTO под shared ``ext.chromadb.persist_path``."""

    persist_path: str


class ChromadbPersistSection(ConfigSection[ChromadbPersistConfig]):
    """Локальная мини-секция: только persist_path под ext.chromadb."""

    id: ClassVar[StrId] = StrId("vector_index_chromadb_persist")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[ChromadbPersistConfig]] = ObjectSchema(
        description=(
            "Локальная мини-секция с persist_path для CLI-индексатора. "
            "Дублирует одноимённое поле full ChromadbSection из "
            "boba-ext-chromadb, чтобы CLI работал без установленного "
            "extension'а."
        ),
        fields=[
            FieldSpec(
                name="persist_path",
                coercer=ChainCoercer(ParseString()),
                description=(
                    "Путь к persistent ChromaDB store. Общий с "
                    "boba-ext-chromadb (если установлен)."
                ),
                required=True,
            ),
        ],
        factory=ChromadbPersistConfig,
    )
