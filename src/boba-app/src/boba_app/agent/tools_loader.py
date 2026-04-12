"""ToolsFiller — загрузка tool definitions в context window.

PipelineStage: загружает definitions из registry в window.
"""
from __future__ import annotations

from typing import Iterator

from boba_domain.agent.context_filler import ContextRequest
from boba_domain.agent.events import DocPipelineEvent, StageCompleted, StageStarted
from boba_domain.core.tools import ToolRegistry
from boba_domain.core.pipeline import PipelineStage


class ToolsFiller(PipelineStage[ContextRequest, DocPipelineEvent]):
    """PipelineStage: загружает tool definitions из registry в window."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "tools"

    def run(self, ctx: ContextRequest) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)
        definitions = self._registry.definitions
        ctx.window.set_tool_definitions(definitions)
        yield StageCompleted(stage=self.name, detail=f"{len(definitions)} tools")
