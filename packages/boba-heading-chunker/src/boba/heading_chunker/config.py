"""Конфиг-секция [indexer.chunkers.heading]."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import ChainCoercer, Default, MinValue, ParseInt
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "HeadingChunkerConfig",
    "HeadingChunkerConfigSection",
]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150


@dataclass(frozen=True)
class HeadingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


class HeadingChunkerConfigSection(ConfigSection[HeadingChunkerConfig]):
    """Параметры heading-aware chunker'а."""

    namespace: ClassVar[tuple[str, ...]] = ("indexer", "chunkers", "heading")

    schema: ClassVar[ObjectSchema[HeadingChunkerConfig]] = ObjectSchema(
        description=(
            "Heading-aware chunker: каждая Section даёт ≥1 чанк, sub-split "
            "только внутри одной Section, anchor наследуется в каждом чанке."
        ),
        fields=[
            FieldSpec(
                name="chunk_size",
                coercer=ChainCoercer(
                    Default(DEFAULT_CHUNK_SIZE), ParseInt(), MinValue(1)
                ),
                description=(
                    f"Целевой размер чанка в символах (default {DEFAULT_CHUNK_SIZE}). "
                    "Section короче — один чанк на всю Section. Длиннее — sub-split."
                ),
            ),
            FieldSpec(
                name="chunk_overlap",
                coercer=ChainCoercer(Default(DEFAULT_CHUNK_OVERLAP), ParseInt()),
                description=(
                    f"Перекрытие между sub-чанками одной Section в символах "
                    f"(default {DEFAULT_CHUNK_OVERLAP}). Не пересекает Section-границ."
                ),
            ),
        ],
        factory=HeadingChunkerConfig,
    )
