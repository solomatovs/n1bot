"""boba-processing: generic streaming-processing primitives.

Источник/Transport/Decoder/Reader/Section/RawDocument — всё, что не зависит
от индексации, store или chunking. Используется и индексирующим pipeline'ом
(boba-indexing), и runtime-tools (Confluence agent-tools), и любыми другими
потоковыми обработчиками контента.
"""

from __future__ import annotations

from boba.processing.auth import AuthApplier
from boba.processing.context import IndexingContext, PipelineId
from boba.processing.decoder import Decoder, DecoderId, IdentityDecoder
from boba.processing.errors import (
    IncompatibleContentError,
    IndexingError,
    SyncUnsupportedError,
)
from boba.processing.raw_document import BinaryStream, RawDocument
from boba.processing.reader import Reader, ReaderId
from boba.processing.request import Request
from boba.processing.request_source import RequestSource
from boba.processing.sections import Section
from boba.processing.transport import Transport

__all__ = [
    "AuthApplier",
    "BinaryStream",
    "Decoder",
    "DecoderId",
    "IdentityDecoder",
    "IncompatibleContentError",
    "IndexingContext",
    "IndexingError",
    "PipelineId",
    "RawDocument",
    "Reader",
    "ReaderId",
    "Request",
    "RequestSource",
    "Section",
    "SyncUnsupportedError",
    "Transport",
]
