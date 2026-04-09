"""Фабрика doc-пайплайна и контекстов."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from adapters.chromadb_vectorstore import ChromaVectorStoreService
from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.stages import (
    GenerateStage,
    IndexStage,
    ReadContextStage,
    SearchStage,
)
from domain.pipeline import Pipeline

if TYPE_CHECKING:
    from infrastructure.bootstrap import AppServices


def create_doc_pipeline(services: AppServices) -> Pipeline:
    """Создать 4-стадийный pipeline для чата по документам."""
    return Pipeline([
        IndexStage(),
        SearchStage(),
        ReadContextStage(),
        GenerateStage(services.openai_client),
    ])


def create_doc_context(
    folder_path: Path,
    query: str,
    model: str,
    services: AppServices,
    *,
    top_k: int = 5,
    context_expand_lines: int = 20,
) -> DocPipelineContext:
    """Создать контекст для doc-пайплайна."""
    cfg = services.cfg
    boba_path = cfg.boba_path(folder_path)
    boba_path.mkdir(exist_ok=True)

    chroma_path = str(cfg.chroma_path(folder_path))
    folder_vectorstore = ChromaVectorStoreService(
        db_path=chroma_path,
        default_embedding=services.embeddings,
        cfg=cfg,
    )

    return DocPipelineContext(
        folder_path=folder_path,
        query=query,
        model=model,
        embedding_model=cfg.embedding_model,
        cfg=cfg,
        vectorstore_service=folder_vectorstore,
        top_k=top_k,
        context_expand_lines=context_expand_lines,
    )
