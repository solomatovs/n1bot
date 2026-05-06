"""PipelineSpec + PipelineRegistry: декларативные фабрики IndexPipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from boba.config.app import AppConfig
from boba.config.section import ConfigSection
from boba.indexing.errors import UnknownPipelineError
from boba.indexing.pipeline import IndexPipeline
from boba.processing import PipelineId

__all__ = ["PipelineRegistry", "PipelineSpec"]


@dataclass(frozen=True)
class PipelineSpec:
    """Декларативная фабрика IndexPipeline: секция конфига + сборщик из AppConfig."""

    section: ConfigSection[Any]
    build: Callable[[AppConfig], IndexPipeline[Any]]


class PipelineRegistry:
    """Каталог pipeline-плагинов по `PipelineId`."""

    def __init__(self) -> None:
        self._entries: dict[PipelineId, PipelineSpec] = {}

    def register(self, pipeline_id: PipelineId, spec: PipelineSpec) -> None:
        self._entries[pipeline_id] = spec

    def entries(self) -> dict[PipelineId, PipelineSpec]:
        return dict(self._entries)

    def get(self, pipeline_id: PipelineId) -> PipelineSpec:
        spec = self._entries.get(pipeline_id)
        if spec is None:
            raise UnknownPipelineError(
                f"unknown pipeline_id={pipeline_id.to_wire()!r}; "
                f"registered={[i.to_wire() for i in self._entries]}",
            )
        return spec

    def __contains__(self, pipeline_id: object) -> bool:
        return pipeline_id in self._entries
