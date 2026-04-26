"""Конфиг boba-cli-vector-index как ConfigSection.

Все параметры CLI — поля одной секции ``vector_index``: action
(index/list/delete), paths, collection, description, chunk_size,
chunk_overlap, confirm_skip, verbose. Имена флагов и env-ключей
вычисляются из ConfigKey'ев теми же алгоритмами, что и для остальных
секций (cli_flag_name, env_name, toml_path).

Дополнительно используется shared-ключ
``ConfigKey("ext","chromadb","persist_path")`` — общий с
:mod:`boba.ext.chromadb.config.ChromadbSection`. Импортно CLI на
extension не зависит — оператор может запустить индексирование на
машине, где extension не установлен; контракт — через ConfigKey.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.cli.vector_index.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
)
from boba.config.cli import CliSource, cli_flag_name
from boba.config.env import EnvFileSource, EnvSource, env_name
from boba.config.toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
)
from boba.domain.core.config import (
    ChainedConfigResolver,
    ConfigKey,
    ConfigSection,
    FieldSpec,
    ObjectSchema,
    read_field,
)
from boba.domain.core.patterns import ConverterInputError, StrId
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
    "CliConfigError",
    "VectorIndexConfig",
    "VectorIndexSection",
    "build_resolver",
    "load_persist_path",
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
# Shared chromadb persist_path (ad-hoc, без импорта boba-ext-chromadb)
# ──────────────────────────────────────────────────────────────────────


_PERSIST_KEY = ConfigKey("ext", "chromadb", "persist_path")
_PERSIST_PATH: FieldSpec[str] = FieldSpec(
    name="persist_path",
    converter=ChainConverter(Required(), ParseString()),
)


class CliConfigError(Exception):
    """Ошибка конфига: невалидное состояние per-action или отсутствует
    обязательное поле (например, persist_path).
    """


def build_resolver() -> ChainedConfigResolver:
    """Стандартная цепочка автономных источников.

    Используется и сборкой bundle'а (через ConfigFactory), и ad-hoc
    чтением shared-ключа ``ext.chromadb.persist_path``.
    """
    return ChainedConfigResolver(
        [
            CliSource(),
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(),
            TomlSource(),
        ]
    )


def load_persist_path(resolver: ChainedConfigResolver) -> str:
    """Достаёт shared chromadb persist_path из готового резолвера.

    Поднимает :class:`CliConfigError` с человекочитаемым сообщением,
    указывающим все три способа задать путь (CLI/env/TOML).
    """
    try:
        return read_field(_PERSIST_KEY, _PERSIST_PATH, resolver)
    except ConverterInputError as e:
        raise CliConfigError(
            f"persist_path is required: pass {cli_flag_name(_PERSIST_KEY)}, "
            f"set env {env_name(_PERSIST_KEY)}, или укажи "
            f"[ext.chromadb] persist_path в TOML, на который указывает "
            f"{CONFIG_PATH_ENV}"
        ) from e
