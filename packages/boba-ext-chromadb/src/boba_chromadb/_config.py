"""Конфигурация ChromaDB-extension.

:class:`ChromadbSection` — :class:`~boba.domain.core.config.ConfigSection`,
объявляющая поля расширения через :class:`FieldSpec` поверх
:class:`ConfigKey`. Регистрируется в :class:`ConfigFactory` через
entry-point group ``boba.config_sections`` (см. ``pyproject.toml``);
при сборке :class:`ConfigBundle` секция читает значения через резолвер,
собранный bootstrap'ом приложения, и строит типизированный
:class:`ChromaExtConfig`.

Внутри tool'ов конфиг достаётся через
``ctx.config.section(ChromadbSection)`` в :func:`register_tools`.

Семантика поля ``embedding_model`` в v0.1: поддерживается только
``default`` — встроенная ONNX-модель ChromaDB (валидация через
:class:`OneOf`). Поле оставлено в конфиге как явный контракт на будущее
(когда добавим поддержку ``sentence-transformers`` через optional dep),
чтобы оператор не настраивал «бессмысленную» переменную.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.domain.core.config import (
    REQUIRED,
    ChainedConfigResolver,
    ConfigKey,
    ConfigSection,
    FieldSpec,
    IntConverter,
    StrConverter,
)
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import MinValue, OneOf

__all__ = ["ChromaExtConfig", "ChromadbSection"]


@dataclass(frozen=True)
class ChromaExtConfig:
    persist_path: str
    embedding_model: str
    max_top_k: int
    snippet_chars: int


class ChromadbSection(ConfigSection[ChromaExtConfig]):
    """Секция конфига расширения chromadb. Регистрируется через
    entry-point ``boba.config_sections``.
    """

    id: ClassVar[StrId] = StrId("ext.chromadb")

    PERSIST_PATH = FieldSpec(
        key=ConfigKey("ext", "chromadb", "persist_path"),
        converter=StrConverter(),
        default=REQUIRED,
        description=(
            "Путь к persistent ChromaDB (общий с boba-cli-vector-index, "
            "чтобы агент видел свежепроиндексированные коллекции)."
        ),
    )
    EMBEDDING_MODEL = FieldSpec(
        key=ConfigKey("ext", "chromadb", "embedding_model"),
        converter=StrConverter(),
        default="default",
        validator=OneOf("default"),
        description=(
            "Модель эмбеддингов. v0.1: только 'default' (built-in ONNX). "
            "Расширим, когда добавим sentence-transformers как optional dep."
        ),
    )
    MAX_TOP_K = FieldSpec(
        key=ConfigKey("ext", "chromadb", "max_top_k"),
        converter=IntConverter(),
        default=20,
        validator=MinValue(1),
        description="Жёсткий потолок top_k для kb_search (защита от дикого LLM).",
    )
    SNIPPET_CHARS = FieldSpec(
        key=ConfigKey("ext", "chromadb", "snippet_chars"),
        converter=IntConverter(),
        default=300,
        validator=MinValue(1),
        description="Максимальная длина сниппета документа в результате kb_search.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (
        PERSIST_PATH,
        EMBEDDING_MODEL,
        MAX_TOP_K,
        SNIPPET_CHARS,
    )

    def build(self, resolver: ChainedConfigResolver) -> ChromaExtConfig:
        return ChromaExtConfig(
            persist_path=self.PERSIST_PATH.read(resolver),
            embedding_model=self.EMBEDDING_MODEL.read(resolver),
            max_top_k=self.MAX_TOP_K.read(resolver),
            snippet_chars=self.SNIPPET_CHARS.read(resolver),
        )
