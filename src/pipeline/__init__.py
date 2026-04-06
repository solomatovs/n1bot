"""Pipeline — generic конвейер обработки с наблюдаемостью."""
from pipeline.events import StageCompleted, StageStarted
from pipeline.pipeline import Pipeline
from pipeline.protocol import PipelineStage

__all__ = [
    "Pipeline",
    "PipelineStage",
    "StageCompleted",
    "StageStarted",
]
