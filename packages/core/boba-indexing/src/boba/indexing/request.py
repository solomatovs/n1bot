"""Request (Protocol для request-DTO) и RequestSource — источник Request-планов для Transport'а."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.metadata import Metadata

__all__ = ["Request", "RequestSource"]


@runtime_checkable
class Request(Protocol):
    """Контракт Request-DTO — чистый план «что забрать» + исходная metadata.

    source_id НЕ часть Request — его вычисляет Transport из реального адреса, чтобы identity не дрейфовала.
    """

    @property
    def metadata(self) -> Metadata: ...


ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(ABC, Generic[ReqT]):
    """Источник Request'ов для Transport'а; source_id не формирует — его выводит Transport."""

    @abstractmethod
    def requests(self) -> Iterable[ReqT]:
        """Сгенерировать поток ReqT-планов для Transport'а."""
        ...
