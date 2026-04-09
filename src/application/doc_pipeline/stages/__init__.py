"""Стадии doc-пайплайна."""
from application.doc_pipeline.stages.index import IndexStage
from application.doc_pipeline.stages.search import SearchStage
from application.doc_pipeline.stages.read_context import ReadContextStage
from application.doc_pipeline.stages.generate import GenerateStage

__all__ = [
    "IndexStage",
    "SearchStage",
    "ReadContextStage",
    "GenerateStage",
]
