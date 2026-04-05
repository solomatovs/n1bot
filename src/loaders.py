"""Иерархия загрузчиков Confluence и пайплайн импорта в ChromaDB.

Архитектура (композиция снизу вверх):
    PageLoader          — загрузка одной страницы
    BatchPageLoader     — загрузка списка страниц (использует PageLoader)
    SpaceLoader         — загрузка пространства (использует BatchPageLoader)
    IngestionService    — чанкинг + сохранение (source-agnostic)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Protocol

import requests
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat
from langchain_core.documents import Document

from chunking import ChunkingStrategy  # noqa: TCH001
from errors import (
    IngestionError,
    PageLoadError,
    SpaceEnumerationError,
)
from ui.state import AppConfig, SpaceLoadParams, StorageParams
from vectorstore import VectorStoreService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Результаты
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    """Результат загрузки одной страницы."""
    page_id: str
    documents: List[Document]


@dataclass
class BatchResult:
    """Результат загрузки нескольких страниц."""
    loaded: List[Document]
    failed: List[PageLoadError] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return len(self.loaded)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


@dataclass
class IngestionResult:
    """Результат пайплайна импорта."""
    input_documents: int
    chunks_stored: int


# ---------------------------------------------------------------------------
# Прогресс (Protocol — отвязан от Streamlit)
# ---------------------------------------------------------------------------

class ProgressCallback(Protocol):
    """Протокол для отображения прогресса загрузки."""

    def on_page(self, index: int, total: int, page_id: str, status: str) -> None: ...

    def on_complete(self) -> None: ...


class NoOpProgress:
    """Заглушка прогресса — ничего не делает."""

    def on_page(self, index: int, total: int, page_id: str, status: str) -> None:
        pass

    def on_complete(self) -> None:
        pass


# ---------------------------------------------------------------------------
# PageLoader — загрузка одной страницы
# ---------------------------------------------------------------------------

class PageLoader:
    """Загружает одну страницу Confluence по ID.

    Самый нижний уровень иерархии загрузчиков.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self._base_url = cfg.confluence_url
        self._token = cfg.confluence_token

    def load(self, page_id: str) -> PageResult:
        """Загрузить одну страницу.

        Raises:
            PageLoadError: если страницу не удалось загрузить.
        """
        try:
            loader = ConfluenceLoader(
                url=self._base_url,
                token=self._token,
                include_attachments=False,
                keep_markdown_format=True,
                content_format=ContentFormat.EXPORT_VIEW,
                page_ids=[page_id],
                confluence_kwargs={"verify_ssl": False},
                limit=1,
            )
            docs = loader.load()
            return PageResult(page_id=page_id, documents=docs)
        except Exception as e:
            raise PageLoadError(page_id, e) from e


# ---------------------------------------------------------------------------
# BatchPageLoader — загрузка списка страниц
# ---------------------------------------------------------------------------

class BatchPageLoader:
    """Загружает несколько страниц, делегируя PageLoader.

    Собирает результаты и ошибки, не прерываясь при сбое отдельных страниц.
    """

    def __init__(self, page_loader: PageLoader) -> None:
        self._page_loader = page_loader

    def load(
        self,
        page_ids: List[str],
        progress: ProgressCallback | None = None,
    ) -> BatchResult:
        """Загрузить все страницы из списка."""
        cb = progress or NoOpProgress()
        total = len(page_ids)
        all_docs: List[Document] = []
        errors: List[PageLoadError] = []

        for idx, pid in enumerate(page_ids, start=1):
            try:
                result = self._page_loader.load(pid)
                if result.documents:
                    all_docs.extend(result.documents)
                    cb.on_page(idx, total, pid, f"загружено ({len(result.documents)} док.)")
                else:
                    cb.on_page(idx, total, pid, "пусто")
            except PageLoadError as e:
                log.warning("Не удалось загрузить страницу %s: %s", pid, e.cause)
                errors.append(e)
                cb.on_page(idx, total, pid, f"ошибка: {e.cause}")

        cb.on_complete()
        return BatchResult(loaded=all_docs, failed=errors)


# ---------------------------------------------------------------------------
# SpaceLoader — загрузка пространства
# ---------------------------------------------------------------------------

class SpaceLoader:
    """Загружает все страницы из пространства Confluence.

    Перечисляет ID страниц через REST API, затем делегирует BatchPageLoader.
    """

    def __init__(
        self,
        batch_loader: BatchPageLoader,
        cfg: AppConfig,
        params: SpaceLoadParams,
    ) -> None:
        self._batch_loader = batch_loader
        self._base_url = cfg.confluence_url
        self._token = cfg.confluence_token
        self._params = params

    def load(
        self,
        space_key: str,
        progress: ProgressCallback | None = None,
    ) -> BatchResult:
        """Загрузить все страницы пространства.

        Raises:
            SpaceEnumerationError: если не удалось получить список страниц.
        """
        page_ids = self._enumerate_page_ids(space_key)

        if self._params.max_pages is not None and self._params.max_pages > 0:
            page_ids = page_ids[:self._params.max_pages]

        return self._batch_loader.load(page_ids, progress)

    def _enumerate_page_ids(self, space_key: str) -> List[str]:
        """Получить ID всех страниц пространства через REST API.

        Raises:
            SpaceEnumerationError: при ошибке HTTP или парсинга.
        """
        try:
            return self._paginate_space(space_key)
        except (requests.RequestException, KeyError, ValueError) as e:
            raise SpaceEnumerationError(space_key, e) from e

    def _paginate_space(self, space_key: str) -> List[str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        limit = self._params.api_page_limit
        start = 0
        ids: List[str] = []

        while True:
            params = {"spaceKey": space_key, "type": "page", "limit": limit, "start": start}
            r = requests.get(
                f"{self._base_url.rstrip('/')}/rest/api/content",
                headers=headers,
                params=params,
                verify=False,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json() or {}
            results = data.get("results", []) or []
            if not results:
                break
            ids.extend(str(it["id"]) for it in results if "id" in it)
            if len(results) < limit:
                break
            start += len(results)

        return ids


# ---------------------------------------------------------------------------
# Нормализация документов
# ---------------------------------------------------------------------------

class DocumentNormalizer:
    """Обогащение документов метаданными Confluence."""

    @staticmethod
    def normalize(docs: List[Document], space_key: str, page_id: str, base_url: str) -> List[Document]:
        """Добавить метаданные Confluence к каждому документу."""
        url = f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"
        out: List[Document] = []
        for d in docs:
            md = dict(getattr(d, "metadata", {}) or {})
            md.setdefault("type", "original")
            md["source"] = "confluence"
            md["space_key"] = space_key
            md["page_id"] = page_id
            md.setdefault("url", url)
            out.append(Document(page_content=d.page_content, metadata=md))
        return out

    @staticmethod
    def make_chunk_ids(space_key: str, page_id: str, count: int) -> List[str]:
        """Сгенерировать детерминированные ID чанков."""
        return [f"{space_key}-{page_id}-{i:03d}" for i in range(count)]


# ---------------------------------------------------------------------------
# IngestionService — чанкинг + сохранение (source-agnostic)
# ---------------------------------------------------------------------------

class IngestionService:
    """Пайплайн: документы -> чанки -> ChromaDB.

    Не зависит от источника данных — принимает list[Document].
    """

    def __init__(self, chunker: ChunkingStrategy, vs_service: VectorStoreService) -> None:
        self._chunker = chunker
        self._vs = vs_service

    def ingest(
        self,
        documents: List[Document],
        collection_name: str,
        storage_params: StorageParams,
    ) -> IngestionResult:
        """Разбить документы на чанки и сохранить в ChromaDB.

        Raises:
            IngestionError: если не удалось сохранить ни одного чанка.
            EmbeddingConnectionError: если не удалось подключиться к эмбеддингам.
            DocumentStorageError: если все батчи завершились ошибкой.
        """
        chunks = self._chunker.split_documents(documents)
        if not chunks:
            raise IngestionError("Чанкинг не произвёл ни одного документа")

        store = self._vs.store_documents(
            chunks,
            collection_name=collection_name,
            batch_size=storage_params.batch_size,
        )
        stored_count = len(store.get().get("documents", []) or []) if store else 0
        return IngestionResult(input_documents=len(documents), chunks_stored=stored_count)
