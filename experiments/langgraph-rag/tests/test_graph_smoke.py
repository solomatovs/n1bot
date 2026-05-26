"""Дымовой тест: граф собирается и проходит обе ветки маршрутизации."""

from typing import Any
from unittest.mock import MagicMock

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable
from langgraph_rag.pipeline import RagPipeline, _Category, _Keywords
from pydantic import Field


class _StaticRunnable(Runnable):
    """Возвращает фиксированное значение на любой вход; для mock-LLM в RunnableSequence."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def invoke(self, _input: Any, config: Any = None, **_kwargs: Any) -> Any:
        return self._value

    async def ainvoke(self, _input: Any, config: Any = None, **_kwargs: Any) -> Any:
        return self._value


class _FakeRetriever(BaseRetriever):
    """Возвращает заранее заданный список Document'ов."""

    docs: list[Document] = Field(default_factory=list)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        return self.docs


class TestGraphExecution:
    @staticmethod
    def _build_pipeline(
        category: str,
        keywords: list[str],
        reformulations: list[str],
        hits: list[Document],
        answer: str,
    ) -> RagPipeline:
        classifier = MagicMock()
        classifier.with_structured_output.return_value = _StaticRunnable(
            _Category(value=category)  # type: ignore[arg-type]
        )

        keyword_extractor = MagicMock()
        keyword_extractor.with_structured_output.return_value = _StaticRunnable(
            _Keywords(keywords=keywords, reformulations=reformulations)
        )

        answerer = _StaticRunnable(AIMessage(content=answer))
        retriever = _FakeRetriever(docs=hits)

        return RagPipeline(
            classifier=classifier,
            keyword_extractor=keyword_extractor,
            answerer=answerer,  # type: ignore[arg-type]
            retriever=retriever,
        )

    async def test_general_path_routes_to_direct_answer(self) -> None:
        pipeline = self._build_pipeline(
            category="general",
            keywords=[],
            reformulations=[],
            hits=[],
            answer="прямой ответ",
        )
        graph = pipeline.build_graph()
        state = await graph.ainvoke({"query": "привет"})
        assert state["answer"] == "прямой ответ"
        assert state["category"] == "general"

    async def test_search_path_runs_full_pipeline(self) -> None:
        hits = [Document(page_content="чанк один", metadata={"source": "doc-a"})]
        pipeline = self._build_pipeline(
            category="search",
            keywords=["api", "rate", "limit"],
            reformulations=["лимит запросов API", "ограничение скорости"],
            hits=hits,
            answer="синтез на основе контекста",
        )
        graph = pipeline.build_graph()
        state = await graph.ainvoke({"query": "сколько запросов в минуту?"})
        assert state["answer"] == "синтез на основе контекста"
        assert state["category"] == "search"
        assert state["keywords"] == ["api", "rate", "limit"]
        assert state["hits"] == hits
