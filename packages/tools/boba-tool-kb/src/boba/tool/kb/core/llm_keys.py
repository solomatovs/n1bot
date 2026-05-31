"""LlmKeys — унифицированные `llm.*` metadata-ключи для выдачи LLM.

Любой KB-индексатор (kbdoc / confluence / …) хранит разные native-ключи под
своими namespace'ами (`transport.fs.*`, `confluence.*`, `reader.kbdoc.*`).
Чтобы search мог отдавать LLM однотипный, source-агностичный набор полей,
`LlmMetadataChunker` проецирует native-ключи в эти `llm.*` на ingest'е.

Контракт: всё, что начинается с `LlmKeys.PREFIX`, — это поля, предназначенные
для показа LLM при анализе чанков (vector + полнотекстовый поиск).
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["LlmKeys"]


class LlmKeys:
    """Унифицированные `llm.*` ключи, проставляемые `LlmMetadataChunker`."""

    PREFIX: ClassVar[str] = "llm."
    """Префикс llm-facing полей; search отдаёт LLM всё под этим namespace'ом."""

    PAGE_TITLE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="llm.page_title",
        decode=str,
        encode=str,
    )
    """Заголовок документа (о чём документ)."""

    SOURCE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="llm.source",
        decode=str,
        encode=str,
    )
    """Откуда взять оригинал: URL (+`#anchor` секции, если есть) для re-fetch."""

    PAGE_ID: ClassVar[MetadataKey[str]] = MetadataKey(
        name="llm.page_id",
        decode=str,
        encode=str,
    )
    """ID исходной страницы (confluence pageId / kbdoc page_id) для re-fetch."""

    LOCATION: ClassVar[MetadataKey[str]] = MetadataKey(
        name="llm.location",
        decode=str,
        encode=str,
    )
    """Где внутри документа находится чанк (breadcrumb секции)."""

    TAGS: ClassVar[MetadataKey[str]] = MetadataKey(
        name="llm.tags",
        decode=str,
        encode=str,
    )
    """Тэги чанка, сериализованные как `tag1, tag2` (порядок стабилен)."""

    SPACE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="llm.space",
        decode=str,
        encode=str,
    )
    """Space/namespace источника (confluence space_key / kbdoc space)."""
