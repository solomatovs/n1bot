"""Стадии doc-пайплайна."""
from application.doc_pipeline.stages.generate import GenerateStage
from application.doc_pipeline.stages.history import HistoryStage
from application.doc_pipeline.stages.index import IndexStage
from application.doc_pipeline.stages.read_context import ReadContextStage
from application.doc_pipeline.stages.search import SearchStage
from application.doc_pipeline.stages.system_prompt import SystemPromptStage
from application.doc_pipeline.stages.user_prompt import UserPromptStage

__all__ = [
    "IndexStage",
    "SearchStage",
    "ReadContextStage",
    "SystemPromptStage",
    "HistoryStage",
    "UserPromptStage",
    "GenerateStage",
]
