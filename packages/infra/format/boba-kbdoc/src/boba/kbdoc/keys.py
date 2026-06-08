"""KbDocKeys — типизированные MetadataKey'и формата KB-документа.

KB-документы оператора — это выгрузки из Confluence, поэтому header-поля
сохраняются под ТЕМИ ЖЕ wire-ключами, что и при прямой confluence-индексации
(source_url, confluence.page_id, confluence.space_key) — так search
читает оба источника одинаково, без сведе́ния. Нераспознанные header-ключи
живут под reader.kbdoc.*.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["KbDocKeys"]


class KbDocKeys:
    """Ключи, проставляемые KbDocReader из header-блока."""

    SOURCE_URL: ClassVar[MetadataKey[str]] = MetadataKey(
        name="source_url",
        decode=str,
        encode=str,
    )
    """Canonical URL источника — для citation/deep-link в search."""

    PARENT_URL: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.parent_url",
        decode=str,
        encode=str,
    )
    """URL родительской страницы (у confluence-вложений) — тот же wire-ключ, что у
    confluence; KbDocReader его не пишет (kb_doc — без вложений), держит форму."""

    PAGE_ID: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.page_id",
        decode=str,
        encode=str,
    )
    """Confluence pageId исходной страницы — тот же wire-ключ, что у confluence."""

    SPACE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.space_key",
        decode=str,
        encode=str,
    )
    """Confluence space — тот же wire-ключ, что у confluence-индексации."""

    VERSION: ClassVar[MetadataKey[int]] = MetadataKey(
        name="confluence.version",
        decode=int,
        encode=str,
    )
    """Confluence version.number из header'а (`version:`) — тот же wire-ключ, что
    у confluence; нужен для сравнения «в индексе устарело vs online». Опционально:
    если в header'е выгрузки нет — поле пустое."""

    CUSTOM_PREFIX: ClassVar[str] = "reader.kbdoc."
    """Префикс для нераспознанных header-ключей: reader.kbdoc.{name}."""
