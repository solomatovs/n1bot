"""Стадии load-пайплайна."""
from application.load_pipeline.stages.load_pages import LoadPagesStage
from application.load_pipeline.stages.chunk import ChunkStage
from application.load_pipeline.stages.store import StoreStage

__all__ = [
    "LoadPagesStage",
    "ChunkStage",
    "StoreStage",
]
