"""Конфигурация ChromaDB-extension.

ChromadbSection регистрируется через entry-point boba.config_sections;
внутри tool'ов конфиг достаётся ctx.config.section(ChromadbSection).

embedding_model в v0.1 валидируется как OneOf("default") — встроенная
ONNX-модель ChromaDB; поле оставлено как контракт на будущее
(sentence-transformers через optional dep).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.domain.core.config import (
    ConfigSection,
    FieldSpec,
    ObjectSchema,
)
from boba.domain.core.patterns import StrId
from boba.domain.core.validators import (
    ChainConverter,
    Default,
    MinValue,
    OneOf,
    ParseInt,
    ParseString,
    Required,
)

__all__ = ["ChromaExtConfig", "ChromadbSection"]


@dataclass(frozen=True)
class ChromaExtConfig:
    persist_path: str
    embedding_model: str
    max_top_k: int
    snippet_chars: int


class ChromadbSection(ConfigSection[ChromaExtConfig]):
    """Секция конфига расширения chromadb. Регистрируется через
    entry-point boba.config_sections.
    """

    id: ClassVar[StrId] = StrId("ext.chromadb")
    namespace: ClassVar[tuple[str, ...]] = ("ext", "chromadb")

    schema: ClassVar[ObjectSchema[ChromaExtConfig]] = ObjectSchema(
        description="Конфигурация ChromaDB-extension: путь к persistent "
        "store, embedding-модель, лимиты kb_search.",
        fields=[
            FieldSpec(
                name="persist_path",
                converter=ChainConverter(Required(), ParseString()),
                description=(
                    "Путь к persistent ChromaDB (общий с boba-cli-vector-index, "
                    "чтобы агент видел свежепроиндексированные коллекции)."
                ),
            ),
            FieldSpec(
                name="embedding_model",
                converter=ChainConverter(
                    Default("default"), ParseString(), OneOf("default"),
                ),
                description=(
                    "Модель эмбеддингов. v0.1: только 'default' (built-in ONNX). "
                    "Расширим, когда добавим sentence-transformers как optional dep."
                ),
            ),
            FieldSpec(
                name="max_top_k",
                converter=ChainConverter(Default(20), ParseInt(), MinValue(1)),
                description="Жёсткий потолок top_k для kb_search "
                "(защита от дикого LLM).",
            ),
            FieldSpec(
                name="snippet_chars",
                converter=ChainConverter(Default(300), ParseInt(), MinValue(1)),
                description="Максимальная длина сниппета документа "
                "в результате kb_search.",
            ),
        ],
        factory=ChromaExtConfig,
    )
