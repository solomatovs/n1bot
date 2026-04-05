from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import requests
import streamlit as st
from langchain_core.documents import Document

from chunking import split_into_chunks_semantic
from config import enc


@dataclass
class DocumentsResult:
    """Результат добавления документов в векторное хранилище."""
    ok: int
    bad: int


@dataclass
class IngestionResult:
    """Результат импорта пространства Confluence."""
    processed_pages: int
    ok_docs: int
    bad_docs: int
from vectorstore import get_vectorstore


def list_space_page_ids(
    base_url: str,
    token: str,
    space_key: str,
    verify_ssl: bool = False,
    page_limit: int = 200,
    timeout: int = 20,
) -> List[str]:
    headers = {"Authorization": f"Bearer {token}"}
    start = 0
    ids: List[str] = []
    while True:
        params = {"spaceKey": space_key, "type": "page", "limit": page_limit, "start": start}
        r = requests.get(
            f"{base_url.rstrip('/')}/rest/api/content",
            headers=headers,
            params=params,
            verify=verify_ssl,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json() or {}
        results = data.get("results", []) or []
        if not results:
            break
        ids.extend(str(it.get("id")) for it in results if it.get("id"))
        if len(results) < page_limit:
            break
        start += len(results)
    return ids


def normalize_as_original(
    docs: List[Document],
    space_key: str,
    page_id: str,
    base_url: Optional[str] = None,
) -> List[Document]:
    out: List[Document] = []
    url = f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}" if base_url else None
    for d in docs:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        md["source"] = "confluence"
        md["space_key"] = space_key
        md["page_id"] = page_id
        if url:
            md.setdefault("url", url)
        out.append(Document(page_content=d.page_content, metadata=md))
    return out


def make_chunk_ids(space_key: str, page_id: str, count: int, suffix: str = "") -> List[str]:
    return [f"{space_key}-{page_id}-{i:03d}{suffix}" for i in range(count)]


def append_documents(
    collection_name: str,
    docs: List[Document],
    ids: List[str],
    db_path: str,
    ollama_api_url: str,
    embedding_model: Optional[str] = None,
) -> DocumentsResult:
    vs = get_vectorstore(collection_name, db_path=db_path, llm_base_url=ollama_api_url, embedding_model=embedding_model)
    ok = 0
    bad = 0
    try:
        vs.add_documents(docs, ids=ids)
        ok += len(docs)
    except Exception:
        for d, _id in zip(docs, ids):
            try:
                vs.add_documents([d], ids=[_id])
                ok += 1
            except Exception:
                bad += 1
    return DocumentsResult(ok=ok, bad=bad)


def ingest_space_incremental(
    base_url: str,
    token: str,
    space_key: str,
    collection_name: str,
    db_path: str,
    ollama_api_url: str,
    summarize: bool = False,
    max_pages: Optional[int] = None,
    verify_ssl: bool = False,
    embedding_model: Optional[str] = None,
) -> IngestionResult:
    from langchain_community.document_loaders import ConfluenceLoader
    from langchain_community.document_loaders.confluence import ContentFormat

    page_ids = list_space_page_ids(base_url, token, space_key, verify_ssl=verify_ssl)
    if not page_ids:
        return IngestionResult(processed_pages=0, ok_docs=0, bad_docs=0)

    if max_pages is not None and max_pages > 0:
        page_ids = page_ids[:max_pages]

    processed_pages = 0
    ok_docs = 0
    bad_docs = 0
    total_pages = len(page_ids)
    pbar = st.progress(0.0, text=f"Готовлюсь… 0 / {total_pages}")

    for idx, pid in enumerate(page_ids, start=1):
        try:
            loader = ConfluenceLoader(
                url=base_url,
                token=token,
                include_attachments=False,
                keep_markdown_format=True,
                content_format=ContentFormat.EXPORT_VIEW,
                page_ids=[pid],
                confluence_kwargs={"verify_ssl": verify_ssl},
                limit=1,
            )
            docs = loader.load()
            if not docs:
                processed_pages += 1
                pbar.progress(idx / total_pages, text=f"Страница {idx}/{total_pages}: пусто")
                continue

            chunks = split_into_chunks_semantic(docs, ollama_api_url=ollama_api_url, tokenizer=enc, max_tokens=500)
            prepared = normalize_as_original(chunks, space_key, pid, base_url=base_url)
            ids = make_chunk_ids(space_key, pid, len(prepared))

            result = append_documents(
                collection_name,
                prepared,
                ids,
                db_path=db_path,
                ollama_api_url=ollama_api_url,
                embedding_model=embedding_model,
            )
            ok_docs += result.ok
            bad_docs += result.bad
            processed_pages += 1

            pbar.progress(
                idx / total_pages,
                text=f"Стр. {idx}/{total_pages} (pageId={pid}) — добавлено {result.ok}, ошибок {result.bad}",
            )
        except Exception as e:
            processed_pages += 1
            pbar.progress(
                idx / total_pages,
                text=f"Стр. {idx}/{total_pages} (pageId={pid}) — ошибка: {e}",
            )

    pbar.empty()
    return IngestionResult(processed_pages=processed_pages, ok_docs=ok_docs, bad_docs=bad_docs)
