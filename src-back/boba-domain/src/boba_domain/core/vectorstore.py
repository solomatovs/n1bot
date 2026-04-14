"""Контракт сервиса векторного хранилища — Protocol для domain/application логики."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class DocumentLike(Protocol):
    """Минимальный контракт документа для domain-логики.

    langchain_core.documents.Document реализует этот Protocol автоматически.
    """

    @property
    def page_content(self) -> str: ...

    @property
    def metadata(self) -> dict: ...


@dataclass(frozen=True)
class CollectionInfo:
    """Информация о коллекции в векторном хранилище."""

    name: str
    embedding_model: Optional[str] = None


@dataclass(frozen=True)
class CollectionData:
    """Содержимое коллекции — идентификаторы, тексты, метаданные."""

    ids: List[str]
    documents: List[str]
    metadatas: List[dict]


@dataclass(frozen=True)
class ScoredDocument:
    """Документ с оценкой релевантности."""

    document: DocumentLike
    score: float


@runtime_checkable
class VectorStoreService(Protocol):
    """Абстракция работы с векторным хранилищем."""

    def list_collections(self) -> List[CollectionInfo]: ...

    def get_collection_data(self, collection_name: str) -> CollectionData: ...

    def get_collection_embedding_model(self, collection_name: str) -> Optional[str]: ...

    def create_collection(self, collection_name: str, embedding_model: str) -> None: ...

    def remove_collection(self, name: str) -> None: ...

    def store_batch(
        self, collection_name: str, docs: Sequence[DocumentLike]
    ) -> int: ...

    def search(
        self, collection_name: str, query: str, k: int, filters: dict
    ) -> List[DocumentLike]: ...

    def search_with_scores(
        self, collection_name: str, query: str, k: int
    ) -> List[ScoredDocument]: ...

    def collection_doc_count(self, collection_name: str) -> int: ...
