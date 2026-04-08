"""Стадии load-пайплайна."""
from load_pipeline.stages.load_pages import LoadPagesStage
from load_pipeline.stages.chunk import ChunkStage
from load_pipeline.stages.store import StoreStage

__all__ = [
    "LoadPagesStage",
    "ChunkStage",
    "StoreStage",
]
