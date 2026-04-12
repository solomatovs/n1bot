"""UserPromptFiller — добавление user query в context window.

PipelineStage: берёт query из ContextRequest, добавляет в window.
"""
from __future__ import annotations

from typing import Iterator

from domain.agent.context_filler import ContextRequest
from domain.agent.events import DocPipelineEvent, StageCompleted, StageStarted
from domain.core.pipeline import PipelineStage


class UserPromptFiller(PipelineStage[ContextRequest, DocPipelineEvent]):
    """PipelineStage: добавляет user query в window."""

    @property
    def name(self) -> str:
        return "user_prompt"

    def run(self, ctx: ContextRequest) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)
        ctx.window.add_user(ctx.query)
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.query)} символов")
