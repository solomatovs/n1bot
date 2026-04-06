"""Стадия 4: RRF-слияние результатов пои��ка."""
from __future__ import annotations

from typing import Iterator

from errors import RetrievalError
from events import ChatEvent
from pipeline.context import PipelineContext
from pipeline.events import StageCompleted, StageStarted
from retrieval import _rrf_merge


class RRFMergeStage:

    @property
    def name(self) -> str:
        return "rrf_merge"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.rank_lists is not None

        if not ctx.search_params.use_multi_query:
            ctx.merged_docs = ctx.rank_lists[0] if ctx.rank_lists else []
        else:
            ctx.merged_docs = _rrf_merge(ctx.rank_lists, k=ctx.retrieval_config.rrf_k)

        if not ctx.merged_docs and ctx.search_errors:
            raise RetrievalError(
                f"Все варианты поиска завершились ошибкой: {'; '.join(ctx.search_errors)}"
            )

        yield StageCompleted(stage=self.name, detail=f"{len(ctx.merged_docs)} документов после слияни��")
