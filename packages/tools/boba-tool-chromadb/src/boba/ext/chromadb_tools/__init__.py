"""Boba extension: read-only ChromaDB knowledge-base tools."""

from __future__ import annotations

from boba.ext.chromadb_tools.plugin import ChromadbPlugin, ChromadbPluginConfig
from boba.ext.chromadb_tools.record_manager import ChromaRecordManager
from boba.ext.chromadb_tools.vector_store import ChromaVectorStore

__all__ = [
    "ChromaRecordManager",
    "ChromaVectorStore",
    "ChromadbPlugin",
    "ChromadbPluginConfig",
]
