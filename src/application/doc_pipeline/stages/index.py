"""Стадия индексации в doc-пайплайне — обёртка над index_pipeline."""
from __future__ import annotations

from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import (
    DocPipelineEvent,
    FileIndexed,
    IndexingDone,
    IndexingSkipped,
)
from application.index_pipeline import (
    IndexContext,
    run_indexing,
)
from application.index_pipeline import (
    FileCompleted as IdxFileCompleted,
    IndexingDone as IdxDone,
    IndexingSkipped as IdxSkipped,
)
from domain.pipeline import StageCompleted, StageStarted


class IndexStage:
    """Индексация как стадия doc-пайплайна.

    Делегирует в run_indexing(), транслирует события
    в формат DocPipelineEvent для совместимости с UI чата.
    """

    @property
    def name(self) -> str:
        return "index"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        idx_ctx = IndexContext(
            source_path=ctx.source_path,
            manifest_path=ctx.manifest_path,
            collection_name=ctx.collection_name,
            embedding_model=ctx.embedding_model,
            vectorstore_service=ctx.vectorstore_service,
        )

        for event in run_indexing(idx_ctx):
            match event:
                case IdxSkipped(collection=c, doc_count=n):
                    yield IndexingSkipped(collection=c, doc_count=n)
                    yield StageCompleted(
                        stage=self.name,
                        detail=f"индекс актуален ({n} чанков)",
                    )
                    return

                case IdxFileCompleted(filename=f, chunks=c, index=i, total=t):
                    yield FileIndexed(filename=f, chunks=c, index=i, total=t)

                case IdxDone(total_files=f, total_chunks=c):
                    yield IndexingDone(total_files=f, total_chunks=c)
                    yield StageCompleted(
                        stage=self.name,
                        detail=f"{f} файлов, {c} чанков",
                    )
