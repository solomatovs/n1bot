"""Конфиг boba-cli-vector-index как ConfigSection.

Все параметры CLI — поля одной секции ``vector_index``: action
(index/list/delete), paths, collection, description, chunk_size,
chunk_overlap, confirm_skip, verbose. Имена флагов и env-ключей
вычисляются из ConfigKey'ев теми же алгоритмами, что и для остальных
секций (cli_flag_name, env_name, toml_path).

Дополнительно регистрируется :class:`ChromadbPersistSection` —
локальная мини-секция с одним полем ``persist_path``, под общим
namespace ``("ext", "chromadb")``. Это даёт CLI работать автономно,
не импортируя ``boba-ext-chromadb`` (extension может быть не
установлен). Если extension всё-таки установлен и зарегистрировал
свою полную ChromadbSection, оба секции читают тот же ConfigKey —
конфликта значений нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.cli.vector_index.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
)
from boba.domain.core.config import (
    ConfigSection,
    FieldSpec,
    ObjectSchema,
)
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import (
    ChainConverter,
    Default,
    Nullable,
    OneOf,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
    Required,
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
    """Параметры одного запуска boba-cli-vector-index.

    action — обязательно; что делать (index/list/delete). Остальные
    поля опциональны; конкретные обязательности per-action валидирует
    handler (например, paths/collection нужны только для index).
    """

    action: str
    paths: list[str] | None
    collection: str | None
    description: str | None
    chunk_size: int
    chunk_overlap: int
    confirm_skip: bool
    verbose: int


class VectorIndexSection(ConfigSection[VectorIndexConfig]):
    """Секция vector_index. Регистрируется напрямую в
    ``boba.cli.vector_index.cli`` (own-section CLI-приложения; не
    third-party extension — entry-point не нужен).
    """

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
                converter=ChainConverter(
                    Required(), ParseString(), OneOf(*sorted(ACTIONS)),
                ),
                description=(
                    f"Что делать: один из {sorted(ACTIONS)}. Обязательно."
                ),
            ),
            FieldSpec(
                name="paths",
                converter=Nullable(ParseCsvList()),
                description=(
                    "Файлы/директории для индексации (CSV в env, "
                    "TOML-array). Обязательно для action=index."
                ),
            ),
            FieldSpec(
                name="collection",
                converter=Nullable(ParseString()),
                description=(
                    "Имя коллекции в ChromaDB. Обязательно для "
                    "action=index/delete."
                ),
            ),
            FieldSpec(
                name="description",
                converter=Nullable(ParseString()),
                description=(
                    "Описание коллекции (видно агенту через "
                    "kb_list_collections). Применяется только при "
                    "создании коллекции."
                ),
            ),
            FieldSpec(
                name="chunk_size",
                converter=ChainConverter(
                    Default(DEFAULT_CHUNK_SIZE), ParseInt(),
                ),
                description=(
                    f"Размер чанка в символах (default {DEFAULT_CHUNK_SIZE})."
                ),
            ),
            FieldSpec(
                name="chunk_overlap",
                converter=ChainConverter(
                    Default(DEFAULT_CHUNK_OVERLAP), ParseInt(),
                ),
                description=(
                    f"Перекрытие чанков в символах "
                    f"(default {DEFAULT_CHUNK_OVERLAP})."
                ),
            ),
            FieldSpec(
                name="confirm_skip",
                converter=ChainConverter(Default(False), ParseBool()),
                description=(
                    "Пропустить интерактивное подтверждение "
                    "(action=delete)."
                ),
            ),
            FieldSpec(
                name="verbose",
                converter=ChainConverter(Default(0), ParseInt()),
                description=(
                    "Verbosity logging: 0=WARN, 1=INFO, 2=DEBUG."
                ),
            ),
        ],
        factory=VectorIndexConfig,
    )


# ──────────────────────────────────────────────────────────────────────
# Shared chromadb persist_path как мини-секция
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChromadbPersistConfig:
    """Минимальный DTO под shared ``ext.chromadb.persist_path``."""

    persist_path: str


class ChromadbPersistSection(ConfigSection[ChromadbPersistConfig]):
    """Локальная мини-секция: только persist_path. Регистрируется
    в ``boba.cli.vector_index.cli`` рядом с VectorIndexSection.

    persist_path помечен Required: отсутствие пути — это блокер для
    индексатора, и framework сам сформирует operator-friendly recipe
    через ConfigFactory.format_config_error (FieldMissingError +
    describe() от каждого подключённого источника). Если установлен
    boba-ext-chromadb, его full ChromadbSection держит то же поле
    под тем же ConfigKey — оба разрешаются в одно значение.
    """

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
                converter=ChainConverter(Required(), ParseString()),
                description=(
                    "Путь к persistent ChromaDB store. Общий с "
                    "boba-ext-chromadb (если установлен)."
                ),
            ),
        ],
        factory=ChromadbPersistConfig,
    )


