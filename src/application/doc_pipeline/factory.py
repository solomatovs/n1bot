"""Фабрика doc-пайплайна и контекстов."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.stages import (
    AgentLoopStage,
    HistoryStage,
    SystemPromptStage,
    UserQueryStage,
)
from domain.pipeline import Pipeline

if TYPE_CHECKING:
    from infrastructure.bootstrap import AppServices


def create_doc_pipeline(services: AppServices) -> Pipeline:
    """Создать агентный pipeline для чата по документам.

    SystemPrompt → History → UserQuery → AgentLoop
    """
    return Pipeline([
        SystemPromptStage(),
        HistoryStage(),
        UserQueryStage(),
        AgentLoopStage(services.openai_client),
    ])


def create_doc_context(
    folder_path: Path,
    query: str,
    model: str,
    services: AppServices,
    *,
    history_path: Path | None = None,
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
        source_path=folder_path,
        source_file_path=lambda filename: folder_path / filename,
        manifest_path=cfg.index_manifest_path(folder_path),
        vectorstore_service=folder_vectorstore,
        history_path=history_path,
    )
