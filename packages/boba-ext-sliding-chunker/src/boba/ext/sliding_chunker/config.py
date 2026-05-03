"""Конфиг-секция [indexer.chunkers.sliding] для SlidingChunker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, MinValue, ParseInt
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "SlidingChunkerConfig",
    "SlidingChunkerConfigSection",
]


DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class SlidingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


class SlidingChunkerConfigSection(ConfigSection[SlidingChunkerConfig]):
    """Параметры character-based sliding chunker'а."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "chunkers", "sliding")

    schema: ClassVar[ObjectSchema[SlidingChunkerConfig]] = ObjectSchema(
        description=(
            "Sliding-window chunker: режет текст Section'а кусками фиксированного "
            "размера с overlap'ом, ища soft-break (\\n\\n, \\n, '. ', ' ')."
        ),
        fields=[
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(
                    Default(DEFAULT_CHUNK_SIZE), ParseInt(), MinValue(1)
                ),
                description=f"Размер чанка в символах (default {DEFAULT_CHUNK_SIZE}).",
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(DEFAULT_CHUNK_OVERLAP), ParseInt()),
                description=(
                    f"Перекрытие чанков в символах (default {DEFAULT_CHUNK_OVERLAP})."
                ),
            ),
        ],
        factory=SlidingChunkerConfig,
    )
