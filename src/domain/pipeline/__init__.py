"""Pipeline — generic конвейер обработки с наблюдаемостью."""
from domain.pipeline.events import StageCompleted, StageStarted
from domain.pipeline.pipeline import Pipeline
from domain.pipeline.protocol import PipelineStage

__all__ = [
    "Pipeline",
    "PipelineStage",
    "StageCompleted",
    "StageStarted",
]
