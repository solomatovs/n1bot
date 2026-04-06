"""Стадии load-пайплайна."""
from load_pipeline.stages.load_pages import LoadPagesStage
from load_pipeline.stages.chunk_and_store import ChunkAndStoreStage

__all__ = [
    "LoadPagesStage",
    "ChunkAndStoreStage",
]
