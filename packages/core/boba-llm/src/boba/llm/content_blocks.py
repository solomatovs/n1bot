"""ContentBlock-классы (для будущей multimodal-поддержки).

Сейчас Message.content — строка. Когда понадобится мультимодал, тип меняется на
``tuple[ContentBlock, ...]`` и эти классы подключаются к иерархии. Файл живёт
здесь как future-shape, классы готовы к использованию, но пока нигде не
ссылаются.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

__all__ = [
    "ContentBlock",
    "ImageBytesBlock",
    "ImageUrlBlock",
    "TextBlock",
]


@dataclass(frozen=True)
class TextBlock:
    """Текстовый сегмент."""

    text: str


@dataclass(frozen=True)
class ImageUrlBlock:
    """Изображение по URL (https:// или data:URL)."""

    url: str


@dataclass(frozen=True)
class ImageBytesBlock:
    """Изображение байтами; mime_type обязателен."""

    data: bytes
    mime_type: str


ContentBlock: TypeAlias = TextBlock | ImageUrlBlock | ImageBytesBlock
