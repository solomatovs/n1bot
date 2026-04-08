from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

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
# Утилиты метаданных документов
# ---------------------------------------------------------------------------

_HEADING_KEYS = ("Header 1", "Header 2")


class DocumentMetadata:
    """Утилиты для работы с метаданными langchain Document."""

    @staticmethod
    def extract(doc: Document) -> dict:
        """Извлечь metadata из Document, гарантируя dict."""
        return dict(getattr(doc, "metadata", None) or {})

    @staticmethod
    def extract_page_id(metadata: dict) -> str:
        """Извлечь page_id из метаданных."""
        return str(metadata.get("page_id") or "unknown")

    @staticmethod
    def extract_heading(metadata: dict) -> str:
        """Извлечь заголовок по приоритету ключей."""
        for key in _HEADING_KEYS:
            value = metadata.get(key)
            if value:
                return str(value)
        return ""

    @staticmethod
    def format_source_line(space: str, pid: str, heading: str, url: str) -> str:
        """Форматировать строку источника для отображения."""
        line = f"- {space}:{pid} {heading}".strip()
        if url:
            line += f" — {url}"
        return line


# ---------------------------------------------------------------------------
# RRF слияние
# ---------------------------------------------------------------------------

def _rrf_merge(rank_lists: Sequence[Sequence[Document]], k: int = RETRIEVAL_CONFIG.rrf_k) -> List[Document]:
    scores: Dict[str, float] = {}
    pick: Dict[str, Document] = {}

    def doc_key(d: Document) -> str:
        md = tuple(sorted(DocumentMetadata.extract(d).items()))
        return f"{hash(d.page_content)}::{hash(md)}"

    for rl in rank_lists:
        for rank, d in enumerate(rl):
            dk = doc_key(d)
            scores[dk] = scores.get(dk, 0.0) + 1.0 / (k + rank + 1)
            pick.setdefault(dk, d)
    ranked_keys = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
    return [pick[dk] for dk in ranked_keys]


def _group_limit_per_page(docs: Sequence[Document], per_page: int) -> List[Document]:
    by: Dict[str, List[Document]] = {}
    for d in docs:
        pid = DocumentMetadata.extract_page_id(DocumentMetadata.extract(d))
        by.setdefault(pid, []).append(d)
    picked: List[Document] = []
    for lst in by.values():
        picked.extend(lst[:per_page])
    return picked


# ---------------------------------------------------------------------------
# Форматирование источников
# ---------------------------------------------------------------------------

def build_sources(docs: Sequence[Document]) -> str:
    """Форматировать список источников для отображения пользователю."""
    lines: List[str] = []
    seen: set = set()
    for d in docs:
        md = DocumentMetadata.extract(d)
        space = md.get("space_key")
        pid = md.get("page_id")
        if not (space and pid):
            continue
        head = DocumentMetadata.extract_heading(md)
        key = (space, pid, head)
        if key in seen:
            continue
        seen.add(key)
        url = md.get("url", "")
        lines.append(DocumentMetadata.format_source_line(space, pid, head, url))
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


def _rerank_results(docs: Sequence[Document], query_type: str) -> List[Document]:
    scored_docs = [
        (doc, _compute_rerank_score(query_type, DocumentMetadata.extract(doc), RETRIEVAL_CONFIG))
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


