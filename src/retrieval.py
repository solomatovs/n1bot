from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from langchain_core.documents import Document

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Конфигурация реранкинга
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetrievalConfig:
    """Параметры алгоритмов слияния и реранкинга."""
    rrf_k: int = 60
    mq_temperature: float = 0.3
    mq_max_tokens: int = 256
    boost_type_match: float = 1.5
    boost_section: float = 1.2
    penalty_token_count: float = 0.8
    min_tokens: int = 50
    max_tokens: int = 1000


RETRIEVAL_CONFIG = RetrievalConfig()


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


