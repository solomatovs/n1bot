"""Фабрика doc-пайплайна и контекстов."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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
    cfg.boba_path(folder_path).mkdir(exist_ok=True)

    chroma_path = str(cfg.chroma_path(folder_path))
    folder_vectorstore = services.create_vectorstore(chroma_path)

    return DocPipelineContext(
        query=query,
        model=model,
        embedding_model=cfg.embedding_model,
        collection_name=cfg.collection_name(folder_path.name),
        context_path=cfg.context_path(folder_path),
        context_file_path=lambda filename: cfg.context_file_path(folder_path, filename),
        manifest_path=cfg.index_manifest_path(folder_path),
        vectorstore_service=folder_vectorstore,
        top_k=top_k,
        context_expand_lines=context_expand_lines,
    )
