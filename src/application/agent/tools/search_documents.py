"""Tool: векторный поиск по проиндексированным документам."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from domain.agent.events import DocPipelineEvent, SearchDone
from domain.core.vectorstore import VectorStoreService
from domain.workspace import Workspace
from domain.di_types import CollectionName
from application.agent.tools._helpers import MAX_RESULT_CHARS, parse_location
from domain.search.types import SearchHit
from domain.core.tools import Tool, ToolEvent, ToolOutput, ToolResult
from domain.search.types import ChunkLocation
from domain.errors import CorruptedIndexError

DocToolOutput = ToolOutput[DocPipelineEvent]


@dataclass(frozen=True)
class SearchParams:
    """Параметры поиска по документам."""
    query: str = field(metadata={"description": "Поисковый запрос"})
    top_k: int = field(default=5, metadata={"description": "Количество результатов (по умолчанию 5)"})


class SearchDocumentsTool(Tool[DocPipelineEvent, SearchParams]):
    """Векторный поиск по проиндексированным документам."""
    
    MAX_RESULT_CHARS = 4000
    _REQUIRED_META_FIELDS = (
        "source_file",
        "start_line",
        "end_line",
        "start_offset",
        "end_offset",
    )

    def __init__(self, ws: Workspace, vs: VectorStoreService, collection_name: CollectionName) -> None:
        self._ws = ws
        self._vs = vs
        self._collection_name = collection_name

    @property
    def name(self) -> str:
        return "search_documents"

    @property
    def description(self) -> str:
        return (
            "Поиск релевантных фрагментов в документах по запросу. "
            "Используй для нахождения информации по теме вопроса. "
            "Можно вызывать несколько раз с разными запросами."
        )

    @property
    def params_type(self) -> type[SearchParams]:
        return SearchParams

    def execute(self, params: SearchParams) -> Iterator[DocToolOutput]:
        results = self._vs.search_with_scores(
            self._collection_name, params.query, params.top_k,
        )

        hits: list[SearchHit] = []
        for scored in results:
            hits.append(SearchHit(
                content=scored.document.page_content,
                location=parse_location(scored.document.metadata),
                score=scored.score,
            ))

        yield ToolEvent(SearchDone(hits=hits))

        if not hits:
            yield ToolResult(content="Ничего не найдено по запросу.")
            return

        parts = (
            f"[{i}] {h.location.source_file}:{h.location.start_line}-{h.location.end_line} "
            f"(секция: {h.location.section_title}, score: {h.score:.2f})\n{h.content}"
            for i, h in enumerate(hits, 1)
        )
        text = "\n\n---\n\n".join(parts)
        yield ToolResult(content=text[:MAX_RESULT_CHARS])

    @classmethod
    def parse_location(cls, meta: dict) -> ChunkLocation:
        """Валидировать метаданные и собрать ChunkLocation."""
        missing = [f for f in cls._REQUIRED_META_FIELDS if f not in meta]
        if missing:
            raise CorruptedIndexError(
                missing_fields=missing,
                source_file=meta.get("source_file", ""),
            )
        return ChunkLocation(
            source_file=meta["source_file"],
            start_line=meta["start_line"],
            end_line=meta["end_line"],
            start_offset=meta["start_offset"],
            end_offset=meta["end_offset"],
            section_title=meta.get("section_title", ""),
        )
