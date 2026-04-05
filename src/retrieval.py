from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import chromadb.errors
from langchain_chroma import Chroma
from langchain_core.documents import Document
from openai import APIError as OpenAIAPIError
from openai import OpenAI

from errors import RetrievalError
from ui.state import SearchParams
from vectorstore import VectorStoreService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Конфигурация реранкинга
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetrievalConfig:
    """Параметры алгоритмов слияния и реранкинга."""
    rrf_k: int = 60
    mq_temperature: float = 0.3
    boost_type_match: float = 1.5
    boost_section: float = 1.2
    penalty_token_count: float = 0.8
    min_tokens: int = 50
    max_tokens: int = 1000


RETRIEVAL_CONFIG = RetrievalConfig()


# ---------------------------------------------------------------------------
# Генерация переформулировок
# ---------------------------------------------------------------------------

def _gen_query_variants(openai: OpenAI, model: str, q: str, n: int) -> List[str]:
    """Сгенерировать переформулировки запроса.

    При ошибке логирует предупреждение и возвращает оригинальный запрос.
    """
    prompt = f"Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {q}"
    try:
        r = openai.chat.completions.create(
            model=model,
            temperature=RETRIEVAL_CONFIG.mq_temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        content = r.choices[0].message.content
        if not content:
            log.warning("Модель вернула пустой ответ при генерации переформулировок")
            return [q]
        lines = [s.strip("- ").strip() for s in content.splitlines() if s.strip()]
        return [q] + lines[:n]
    except OpenAIAPIError as e:
        log.warning("Не удалось сгенерировать переформулировки: %s", e)
        return [q]


# ---------------------------------------------------------------------------
# RRF слияние
# ---------------------------------------------------------------------------

def _rrf_merge(rank_lists: List[List[Document]], k: int = RETRIEVAL_CONFIG.rrf_k) -> List[Document]:
    scores: Dict[str, float] = {}
    pick: Dict[str, Document] = {}

    def doc_key(d: Document) -> str:
        md = tuple(sorted((d.metadata or {}).items()))
        return f"{hash(d.page_content)}::{hash(md)}"

    for rl in rank_lists:
        for rank, d in enumerate(rl):
            dk = doc_key(d)
            scores[dk] = scores.get(dk, 0.0) + 1.0 / (k + rank + 1)
            pick.setdefault(dk, d)
    return [pick[dk] for dk, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def _pid_from_meta(md: Dict) -> str:
    return str(md.get("page_id") or "unknown")


def _group_limit_per_page(docs: List[Document], per_page: int) -> List[Document]:
    by: Dict[str, List[Document]] = {}
    for d in docs:
        pid = _pid_from_meta(d.metadata or {})
        by.setdefault(pid, []).append(d)
    picked: List[Document] = []
    for lst in by.values():
        picked.extend(lst[:per_page])
    return picked


# ---------------------------------------------------------------------------
# Форматирование источников
# ---------------------------------------------------------------------------

def build_sources(docs: List[Document]) -> str:
    lines: List[str] = []
    seen: set = set()
    for d in docs:
        md = d.metadata or {}
        space = md.get("space_key")
        pid = md.get("page_id")
        url = md.get("url") or ""
        if not (space and pid):
            continue
        head = md.get("Header 1") or md.get("Header 2") or ""
        key = (space, pid, head)
        if key in seen:
            continue
        seen.add(key)
        line = f"- {space}:{pid} {head}".strip()
        if url:
            line += f" — {url}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Классификация запроса и реранкинг
# ---------------------------------------------------------------------------

def _classify_query_type(query: str) -> str:
    query_lower = query.lower()
    if any(word in query_lower for word in ["код", "пример", "функция", "метод", "класс"]):
        return "code"
    elif any(word in query_lower for word in ["таблица", "данные", "статистика"]):
        return "table"
    elif any(word in query_lower for word in ["как", "инструкция", "шаги", "сделать"]):
        return "howto"
    elif any(word in query_lower for word in ["что", "определение", "понятие", "такое"]):
        return "fact"
    return "general"


def _is_type_match(query_type: str, chunk_type: str) -> bool:
    """Тип чанка соответствует типу запроса."""
    return query_type == chunk_type and query_type in ("code", "table")


def _has_section_structure(metadata: dict) -> bool:
    """Чанк имеет заголовочную структуру."""
    return metadata.get("section_level", 0) > 0


def _is_token_count_out_of_range(token_count: int, cfg: RetrievalConfig) -> bool:
    """Количество токенов выходит за допустимый диапазон."""
    return token_count < cfg.min_tokens or token_count > cfg.max_tokens


def _compute_rerank_score(query_type: str, metadata: dict, cfg: RetrievalConfig) -> float:
    """Вычислить множитель релевантности для одного документа."""
    score = 1.0
    chunk_type = metadata.get("chunk_type", "")

    match True:
        case _ if _is_type_match(query_type, chunk_type):
            score *= cfg.boost_type_match
        case _:
            pass

    if _has_section_structure(metadata):
        score *= cfg.boost_section

    token_count = metadata.get("token_count", 0)
    if _is_token_count_out_of_range(token_count, cfg):
        score *= cfg.penalty_token_count

    return score


def _rerank_results(docs: List[Document], query_type: str) -> List[Document]:
    scored_docs = [
        (doc, _compute_rerank_score(query_type, doc.metadata or {}, RETRIEVAL_CONFIG))
        for doc in docs
    ]
    scored_docs.sort(key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in scored_docs]


# ---------------------------------------------------------------------------
# Построение фильтра
# ---------------------------------------------------------------------------

def _infer_content_types(query_type: str) -> Optional[List[str]]:
    """Определить типы контента по типу запроса (если пользователь не задал явно)."""
    match query_type:
        case "code":
            return ["code"]
        case "fact":
            return ["text", "paragraph", "list"]
        case _:
            return None


def _build_search_filter(
    content_types: Optional[List[str]],
    query_type: str,
) -> dict:
    """Собрать фильтр для ChromaDB из типов контента и типа запроса."""
    conditions: list[dict] = [{"type": {"$eq": "original"}}]

    effective_types = content_types or _infer_content_types(query_type)
    if effective_types:
        conditions.append({"chunk_type": {"$in": effective_types}})

    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


# ---------------------------------------------------------------------------
# Сервис поиска
# ---------------------------------------------------------------------------

class RetrievalService:
    """Сервис поиска документов в векторном хранилище.

    Инкапсулирует VectorStoreService и OpenAI-клиент,
    чтобы метод search принимал только параметры запроса.
    """

    def __init__(self, vs_service: VectorStoreService, openai_client: OpenAI) -> None:
        self._vs = vs_service
        self._client = openai_client

    def search(
        self,
        collection_name: str,
        query: str,
        model: str,
        params: SearchParams,
    ) -> List[Document]:
        """Найти релевантные документы по запросу.

        Raises:
            RetrievalError: если все варианты поиска завершились ошибкой.
        """
        vectorstore = self._vs.get_vectorstore(collection_name)
        query_type = _classify_query_type(query)
        search_filters = _build_search_filter(params.content_types, query_type)

        if not params.use_multi_query:
            return self._single_query_search(vectorstore, query, params.top_n, search_filters)

        cands = self._multi_query_search(
            vectorstore, query, model, params, search_filters,
        )

        reranked = _rerank_results(cands, query_type)
        return _group_limit_per_page(reranked[:params.top_n], per_page=params.per_page)

    # -- приватные методы ------------------------------------------------------

    @staticmethod
    def _single_query_search(
        vectorstore: Chroma, query: str, k: int, filters: dict,
    ) -> List[Document]:
        retr = vectorstore.as_retriever(search_kwargs={"k": k, "filter": filters})
        return retr.invoke(query) or []

    def _multi_query_search(
        self,
        vectorstore: Chroma,
        query: str,
        model: str,
        params: SearchParams,
        filters: dict,
    ) -> List[Document]:
        """Поиск с переформулировками и RRF-слиянием.

        Raises:
            RetrievalError: если все варианты поиска завершились ошибкой.
        """
        variants = _gen_query_variants(self._client, model, query, n=params.mq_variants)
        rank_lists: List[List[Document]] = []
        errors: list[str] = []

        for v in variants:
            try:
                retr = vectorstore.as_retriever(
                    search_kwargs={"k": params.k_per_variant, "filter": filters},
                )
                results = retr.invoke(v) or []
                rank_lists.append(results)
            except (chromadb.errors.ChromaError, RuntimeError) as e:
                log.warning("Ошибка поиска для варианта '%s': %s", v, e)
                errors.append(f"'{v}': {e}")
                rank_lists.append([])

        merged = _rrf_merge(rank_lists)

        if not merged and errors:
            raise RetrievalError(
                f"Все варианты поиска завершились ошибкой: {'; '.join(errors)}"
            )

        return merged
