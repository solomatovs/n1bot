"""Typed application error hierarchy."""
from __future__ import annotations


class AppError(Exception):
    """Base application error."""


class ValidationError(AppError):
    """Required parameter is missing or invalid."""


# ---------------------------------------------------------------------------
# RAG pipeline
# ---------------------------------------------------------------------------

class RagError(AppError):
    """RAG pipeline error."""


class EmptyContextError(RagError):
    """No relevant documents found in the vector store."""


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

class RetrievalError(AppError):
    """Document retrieval error."""


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------

class VectorStoreError(AppError):
    """Vector store operation error."""


class EmbeddingConnectionError(VectorStoreError):
    """Failed to connect to the embedding service."""


# ---------------------------------------------------------------------------
# Confluence loading
# ---------------------------------------------------------------------------

class PageLoadError(AppError):
    """Failed to load a page from Confluence."""

    def __init__(self, page_id: str, cause: Exception) -> None:
        self.page_id = page_id
        self.cause = cause
        super().__init__(f"Failed to load page {page_id}: {cause}")


class SpaceEnumerationError(AppError):
    """Failed to enumerate pages in a Confluence space."""

    def __init__(self, space_key: str, cause: Exception) -> None:
        self.space_key = space_key
        self.cause = cause
        super().__init__(f"Failed to enumerate pages in space '{space_key}': {cause}")
