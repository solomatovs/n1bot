"""Стадия 8: Сборка контекста из отобранных документов."""
from __future__ import annotations

from typing import Iterator

from events import ChatEvent, RetrievalDone
from pipeline.context import PipelineContext
from pipeline.events import StageCompleted, StageStarted
from retrieval import build_sources


class ContextAssemblyStage:

    @property
    def name(self) -> str:
        return "context_assembly"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.selected_docs is not None

        ctx.context_text = "\n\n".join(d.page_content for d in ctx.selected_docs)
        ctx.sources_block = build_sources(ctx.selected_docs)

        yield RetrievalDone(
            documents_found=len(ctx.selected_docs),
            context=ctx.prompt_params.format_user_message(
                context=ctx.context_text, query=ctx.query,
            ),
            sources_block=ctx.sources_block,
        )
        yield StageCompleted(stage=self.name, detail=f"{len(ctx.selected_docs)} документов в контексте")
