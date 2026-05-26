"""LangGraph-граф: classify → (keywords → hybrid search → synthesize) | direct_answer."""

import asyncio
from typing import Any, ClassVar, Literal

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from langgraph_rag.state import RagState


class _Category(BaseModel):
    """Категория запроса для роутинга в графе."""

    value: Literal["search", "general"]


class _Keywords(BaseModel):
    """Ключевые термины и переформулировки для KB-поиска."""

    keywords: list[str] = Field(description="Ключевые термины из запроса пользователя")
    reformulations: list[str] = Field(description="Альтернативные формулировки запроса")


class RagPipeline:
    """Сборка LangGraph-графа RAG-workflow; узлы — методы класса."""

    _CATEGORY_SEARCH: ClassVar[str] = "search"
    _CATEGORY_GENERAL: ClassVar[str] = "general"

    _CLASSIFY_PROMPT: ClassVar[str] = (
        "Ты — классификатор запросов. Определи категорию запроса пользователя.\n"
        "- 'search' — запрос требует поиска в базе знаний "
        "(факты, документы, процессы, конкретные сведения).\n"
        "- 'general' — общий вопрос, не требующий поиска "
        "(приветствие, метавопрос о боте, общая логика без специфики).\n\n"
        "Запрос: {query}"
    )

    _KEYWORDS_PROMPT: ClassVar[str] = (
        "Ты — помощник поиска в базе знаний. Из запроса пользователя выдели:\n"
        "- keywords: 3–7 ключевых терминов для поиска;\n"
        "- reformulations: 2–3 переформулировки запроса другими словами.\n\n"
        "Запрос: {query}"
    )

    _SYNTHESIZE_PROMPT: ClassVar[str] = (
        "Ты — ассистент, отвечающий на основе предоставленного контекста.\n"
        "Если в контексте нет ответа — честно скажи об этом.\n"
        "Указывай источники из метаданных, если они есть.\n\n"
        "Контекст:\n{context}\n\n"
        "Запрос: {query}"
    )

    _DIRECT_PROMPT: ClassVar[str] = (
        "Ответь на запрос кратко и по делу.\n\nЗапрос: {query}"
    )

    def __init__(
        self,
        classifier: BaseChatModel,
        keyword_extractor: BaseChatModel,
        answerer: BaseChatModel,
        retriever: BaseRetriever,
    ) -> None:
        self._classifier = classifier.with_structured_output(_Category)
        self._keyword_extractor = keyword_extractor.with_structured_output(_Keywords)
        self._answerer = answerer
        self._retriever = retriever

    async def classify(self, state: RagState) -> dict[str, Any]:
        prompt = ChatPromptTemplate.from_template(self._CLASSIFY_PROMPT)
        result: _Category = await (prompt | self._classifier).ainvoke(
            {"query": state["query"]}
        )
        return {"category": result.value}

    async def extract_keywords(self, state: RagState) -> dict[str, Any]:
        prompt = ChatPromptTemplate.from_template(self._KEYWORDS_PROMPT)
        result: _Keywords = await (prompt | self._keyword_extractor).ainvoke(
            {"query": state["query"]}
        )
        return {
            "keywords": result.keywords,
            "reformulations": result.reformulations,
        }

    async def kb_search(self, state: RagState) -> dict[str, Any]:
        queries = self._build_search_queries(
            query=state["query"],
            keywords=state.get("keywords", []),
            reformulations=state.get("reformulations", []),
        )
        per_query = await asyncio.gather(
            *(self._retriever.ainvoke(q) for q in queries)
        )
        merged: list[Document] = [doc for batch in per_query for doc in batch]
        return {"hits": self._dedup(merged)}

    async def synthesize(self, state: RagState) -> dict[str, Any]:
        prompt = ChatPromptTemplate.from_template(self._SYNTHESIZE_PROMPT)
        context = self._format_context(state.get("hits", []))
        response = await (prompt | self._answerer).ainvoke(
            {"query": state["query"], "context": context}
        )
        return {"answer": response.content}

    async def direct_answer(self, state: RagState) -> dict[str, Any]:
        prompt = ChatPromptTemplate.from_template(self._DIRECT_PROMPT)
        response = await (prompt | self._answerer).ainvoke(
            {"query": state["query"]}
        )
        return {"answer": response.content}

    def _route_by_category(self, state: RagState) -> str:
        category = state.get("category")
        if category == self._CATEGORY_SEARCH:
            return "extract_keywords"
        return "direct_answer"

    @staticmethod
    def _build_search_queries(
        query: str, keywords: list[str], reformulations: list[str]
    ) -> list[str]:
        queries: list[str] = [query]
        if keywords:
            queries.append(" ".join(keywords))
        queries.extend(reformulations)
        return queries

    @staticmethod
    def _dedup(docs: list[Document]) -> list[Document]:
        seen: set[str] = set()
        result: list[Document] = []
        for doc in docs:
            if doc.page_content in seen:
                continue
            seen.add(doc.page_content)
            result.append(doc)
        return result

    @staticmethod
    def _format_context(hits: list[Document]) -> str:
        if not hits:
            return "(контекст пуст)"
        blocks: list[str] = []
        for index, doc in enumerate(hits, start=1):
            source = doc.metadata.get("source") or doc.metadata.get("title") or "—"
            blocks.append(f"[{index}] источник: {source}\n{doc.page_content}")
        return "\n\n".join(blocks)

    def build_graph(self) -> CompiledStateGraph:
        graph: StateGraph = StateGraph(RagState)
        graph.add_node("classify", self.classify)
        graph.add_node("extract_keywords", self.extract_keywords)
        graph.add_node("kb_search", self.kb_search)
        graph.add_node("synthesize", self.synthesize)
        graph.add_node("direct_answer", self.direct_answer)

        graph.add_edge(START, "classify")
        graph.add_conditional_edges(
            "classify",
            self._route_by_category,
            {
                "extract_keywords": "extract_keywords",
                "direct_answer": "direct_answer",
            },
        )
        graph.add_edge("extract_keywords", "kb_search")
        graph.add_edge("kb_search", "synthesize")
        graph.add_edge("synthesize", END)
        graph.add_edge("direct_answer", END)

        return graph.compile()
