from __future__ import annotations

import time
from typing import List, Optional

import streamlit as st
import chromadb
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from config import EMBEDDING_MODEL, secret
from embeddings import LiteLLMEmbeddings


def get_client(db_path: str):  # -> chromadb.PersistentClient
    return chromadb.PersistentClient(path=db_path, settings=Settings(anonymized_telemetry=False))


def get_vectorstore(
    name: str,
    db_path: str,
    llm_base_url: str,
    embedding_model: Optional[str] = None,
) -> Chroma:
    model = embedding_model or EMBEDDING_MODEL
    client = get_client(db_path)
    embedding = LiteLLMEmbeddings(
        model=model,
        base_url=llm_base_url,
        api_key=secret("LITELLM_API_KEY"),
    )
    return Chroma(client=client, collection_name=name, embedding_function=embedding)


def store_to_chroma(
    docs: List[Document],
    collection_name: str,
    db_path: str,
    batch_size: int = 16,
    llm_base_url: str = "",
    embedding_model: Optional[str] = None,
):
    if not llm_base_url:
        llm_base_url = secret("OLLAMA_API_URL")

    api_key: str = secret("LITELLM_API_KEY")

    try:
        test_embeddings = LiteLLMEmbeddings(
            model=embedding_model or EMBEDDING_MODEL,
            base_url=llm_base_url,
            api_key=api_key,
        )
        test_embeddings.embed_query("test")
        st.success("Подключение к liteLLM работает")
    except Exception as e:
        st.error(f"Ошибка подключения к liteLLM: {e}")
        return None

    client = get_client(db_path)
    client.get_or_create_collection(name=collection_name)

    model = embedding_model or EMBEDDING_MODEL
    embedding = LiteLLMEmbeddings(model=model, base_url=llm_base_url, api_key=api_key)
    vectorstore = Chroma(client=client, collection_name=collection_name, embedding_function=embedding)

    def _normalize(d: Document) -> Document:
        md = dict(getattr(d, "metadata", {}) or {})
        md.setdefault("type", "original")
        return Document(page_content=d.page_content, metadata=md)

    docs = [_normalize(d) for d in docs]
    total = len(docs)
    ok, bad = 0, 0
    pbar = st.progress(0.0, text="Начинаю загрузку…")

    for i in range(0, total, batch_size):
        batch = docs[i : i + batch_size]
        try:
            time.sleep(0.5)
            vectorstore.add_documents(batch)
            ok += len(batch)
        except Exception as e:
            st.warning(f"Ошибка на батче {i + 1}-{i + len(batch)}: {e}")
            bad += len(batch)
        finally:
            pbar.progress(
                min((i + batch_size) / max(total, 1), 1.0),
                text=f"Обработано: {min(i + batch_size, total)} / {total}",
            )

    pbar.empty()
    st.success(f"Готово. Успешно: {ok}, с ошибкой: {bad}")
    return client.get_collection(collection_name)


def remove_collection(name: str, db_path: str) -> None:
    client = get_client(db_path)
    client.delete_collection(name)
