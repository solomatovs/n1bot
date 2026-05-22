"""
ConfluenceKeys — MetadataKey, проставляемые Confluence
"""

from __future__ import annotations

import json
from typing import ClassVar

from boba.indexing import MetadataKey
from boba.tool.kb.confluence.attachments import (
    AttachmentInfo,
    decode_attachment,
    decode_attachments,
    encode_attachment,
    encode_attachments,
)

__all__ = ["ConfluenceKeys"]


def _decode_titles(s: str) -> tuple[str, ...]:
    return tuple(str(x) for x in json.loads(s))


def _encode_titles(v: tuple[str, ...]) -> str:
    return json.dumps(list(v), ensure_ascii=False)


class ConfluenceKeys:
    """Confluence-специфичные ключи metadata."""

    PAGE_ID: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.page_id",
        decode=str,
        encode=str,
    )
    HOST: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.host",
        decode=str,
        encode=str,
    )
    VERSION: ClassVar[MetadataKey[int]] = MetadataKey(
        name="confluence.version",
        decode=int,
        encode=str,
    )
    SPACE_KEY: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.space_key",
        decode=str,
        encode=str,
    )
    ANCESTORS_TITLES: ClassVar[MetadataKey[tuple[str, ...]]] = MetadataKey(
        name="confluence.ancestors_titles",
        decode=_decode_titles,
        encode=_encode_titles,
    )
    ATTACHMENTS: ClassVar[MetadataKey[tuple[AttachmentInfo, ...]]] = MetadataKey(
        name="confluence.attachments",
        decode=decode_attachments,
        encode=encode_attachments,
    )
    ATTACHMENT_INFO: ClassVar[MetadataKey[AttachmentInfo]] = MetadataKey(
        name="confluence.attachment_info",
        decode=decode_attachment,
        encode=encode_attachment,
    )
