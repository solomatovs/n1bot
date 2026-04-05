from __future__ import annotations

from typing import Dict, List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from openai import OpenAI


def gen_query_variants(openai: OpenAI, model: str, q: str, n: int = 3) -> List[str]:
    prompt = f"Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {q}"
    try:
        r = openai.chat.completions.create(
            model=model,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        lines = [s.strip("- ").strip() for s in (r.choices[0].message.content or "").splitlines() if s.strip()]
        return [q] + lines[:n]
    except Exception:
        return [q]


def rrf_merge(rank_lists: List[List[Document]], K: int = 60) -> List[Document]:
    scores: Dict[str, float] = {}
    pick: Dict[str, Document] = {}

    def key(d: Document) -> str:
        md = tuple(sorted((d.metadata or {}).items()))
        return f"{hash(d.page_content)}::{hash(md)}"

    for rl in rank_lists:
        for rank, d in enumerate(rl):
            k = key(d)
            scores[k] = scores.get(k, 0.0) + 1.0 / (K + rank + 1)
            pick.setdefault(k, d)
    return [pick[k] for k, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def _pid_from_meta(md: Dict) -> str:
    return str(md.get("page_id") or "unknown")


def group_limit_per_page(docs: List[Document], per_page: int = 1) -> List[Document]:
    by: Dict[str, List[Document]] = {}
    for d in docs:
        pid = _pid_from_meta(d.metadata or {})
        by.setdefault(pid, []).append(d)
    picked: List[Document] = []
    for lst in by.values():
        picked.extend(lst[:per_page])
    return picked


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
# Query classification & reranking
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


def _rerank_results(docs: List[Document], query: str, query_type: str) -> List[Document]:
    scored_docs: List[tuple[Document, float]] = []
    for doc in docs:
        score = 1.0
        metadata = doc.metadata or {}
        chunk_type = metadata.get("chunk_type", "")

        if (query_type == "code" and chunk_type == "code") or (query_type == "table" and chunk_type == "table"):
            score *= 1.5
        if metadata.get("section_level", 0) > 0:
            score *= 1.2
        token_count = metadata.get("token_count", 0)
        if token_count < 50 or token_count > 1000:
            score *= 0.8

        scored_docs.append((doc, score))

    scored_docs.sort(key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in scored_docs]


# ---------------------------------------------------------------------------
# Main retrieval
# ---------------------------------------------------------------------------

def retrieve_docs(
    vectorstore: Chroma,
    query: str,
    use_multi_query: bool,
    openai: Optional[OpenAI],
    llm_model: Optional[str],
    k_single: int = 12,
    mq_variants: int = 3,
    k_per_variant: int = 6,
    total_top: int = 12,
    content_types: Optional[List[str]] = None,
) -> List[Document]:
    query_type = _classify_query_type(query)

    conditions: list = [{"type": {"$eq": "original"}}]
    if content_types:
        conditions.append({"chunk_type": {"$in": content_types}})
    elif query_type == "code":
        conditions.append({"chunk_type": {"$eq": "code"}})
    elif query_type == "fact":
        conditions.append({"chunk_type": {"$in": ["text", "paragraph", "list"]}})

    search_filters = conditions[0] if len(conditions) == 1 else {"$and": conditions}

    if not use_multi_query or openai is None or not llm_model:
        retr = vectorstore.as_retriever(search_kwargs={"k": k_single, "filter": search_filters})
        return retr.invoke(query) or []

    variants = gen_query_variants(openai, llm_model, query, n=mq_variants)
    rank_lists: List[List[Document]] = []
    errors: List[str] = []

    for v in variants:
        try:
            retr = vectorstore.as_retriever(search_kwargs={"k": k_per_variant, "filter": search_filters})
            results = retr.invoke(v) or []
            rank_lists.append(results)
        except Exception as e:
            print(f"Ошибка при поиске для варианта '{v}': {e}")
            errors.append(str(e))
            rank_lists.append([])

    merged = rrf_merge(rank_lists)

    if not merged and errors:
        raise RuntimeError(f"Ошибка поиска по векторной БД: {errors[0]}")

    reranked = _rerank_results(merged, query, query_type)
    return reranked[:total_top]
