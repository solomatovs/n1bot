"""N1 Hub RAG — Streamlit UI."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List

# sqlite hack для некоторых окружений
try:
    import pysqlite3  # type: ignore

    sys.modules["sqlite3"] = sys.modules["pysqlite3"]
except Exception:
    pass

import pandas as pd
import requests
import streamlit as st

import chromadb
from chromadb.config import Settings
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat

from chunking import split_into_chunks_semantic
from config import enc, secret
from confluence import ingest_space_incremental
from rag import prepare_rag_context
from vectorstore import remove_collection, store_to_chroma

# ========================= Page & Config
st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

APP_DB_PATH = Path(secret("CHROMA_DB_PATH")).as_posix()
LITELLM_URL: str = secret("LITELLM_URL")
LITELLM_API_KEY: str = secret("LITELLM_API_KEY")

OLLAMA_API_URL = LITELLM_URL
OLLAMA_OPENAI_URL: str = f"{LITELLM_URL.rstrip('/')}/v1"
KB_URL: str = secret("CONFLUENCE_URL")
CONFLUENCE_T: str = secret("CONFLUENCE_TOKEN")

DEFAULT_COLLECTION: str = secret("DEFAULT_COLLECTION")
DEFAULT_MODEL: str = secret("LLM_MODEL")

# ========================= Cached resources & helpers


@st.cache_resource(show_spinner=False)
def get_chroma_client():  # -> chromadb.PersistentClient
    return chromadb.PersistentClient(path=APP_DB_PATH, settings=Settings(anonymized_telemetry=False))


@st.cache_data(ttl=60, show_spinner=False)
def list_collections() -> List[str]:
    try:
        return [c.name for c in get_chroma_client().list_collections()]
    except Exception as ex:
        st.warning(f"Не удалось получить список коллекций: {ex}")
        return []


@st.cache_data(ttl=60, show_spinner=False)
def get_available_models(ollama_url: str) -> List[str]:
    try:
        r = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=10)
        if r.ok:
            data = r.json() or {}
            names = [m.get("name") for m in data.get("models", [])]
            return names or [DEFAULT_MODEL]
    except Exception:
        pass
    return [DEFAULT_MODEL]


def _fetch_collection_df(
    client,
    collection_name: str,
    preview: bool = False,
) -> pd.DataFrame:
    coll = client.get_collection(collection_name)
    data = coll.get(include=["documents", "metadatas"])
    ids = data.get("ids", [])
    docs = data.get("documents", []) or []
    metas = data.get("metadatas", []) or []
    if preview:
        docs = [d if isinstance(d, str) else str(d) for d in docs]
    return pd.DataFrame({"id": ids, "text": docs, "metadata": metas})


def show_collection_contents(collection_name: str) -> None:
    client = get_chroma_client()
    try:
        df = _fetch_collection_df(client, collection_name, preview=False)
        st.success(f"Документов: {len(df)} в «{collection_name}»")
        st.dataframe(df, height=500)
    except Exception as e:
        st.error(f"Ошибка при получении коллекции: {e}")


@st.cache_data(ttl=20, show_spinner=False)
def get_conn_table_preview(collection_name: str) -> pd.DataFrame:
    try:
        client = get_chroma_client()
        return _fetch_collection_df(client, collection_name, preview=True)
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}")
        return pd.DataFrame({"id": [], "text": [], "metadata": []})


def get_openai_models() -> List[str]:
    try:
        response = requests.get(
            f"{LITELLM_URL}/v1/models",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
        )
        response.raise_for_status()
        models = [model["id"] for model in response.json()["data"]]
        return sorted(models)
    except Exception as e:
        st.error(f"Ошибка получения моделей: {e}")
        return []


def _extract_page_ids_from_answer(ans: str) -> List[str]:
    return re.findall(r"-\s+[^\s:]+:(\d+)\b", ans)


# ========================= Session defaults
if "selected_collection" not in st.session_state:
    st.session_state.selected_collection = DEFAULT_COLLECTION
if "selected_model_name" not in st.session_state:
    st.session_state.selected_model_name = DEFAULT_MODEL
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "available_models" not in st.session_state:
    st.session_state.available_models = get_available_models(OLLAMA_API_URL)
if "last_prompt_base" not in st.session_state:
    st.session_state.last_prompt_base = ""
if "variants" not in st.session_state:
    st.session_state.variants = {}
if "used_page_ids" not in st.session_state:
    st.session_state.used_page_ids = {}

# ======================== UI Tabs
tabChat, tabLoad, tabData = st.tabs(["Чат", "Загрузка из Confluence", "Векторное хранилище"])

# ======================== Tab: Chat
with tabChat:
    st.title("N1 Hub AI bots")

    db_colls = list_collections()
    st.session_state.selected_collection = st.selectbox(
        "Имя векторной БД (коллекция)",
        db_colls or [st.session_state.selected_collection],
        index=(
            db_colls.index(st.session_state.selected_collection)
            if st.session_state.selected_collection in db_colls
            else 0
        ),
        key="select_collection_chat",
    )

    cols = st.columns([3, 1, 1])
    with cols[0]:
        list_models = get_openai_models()
        default_idx = list_models.index(DEFAULT_MODEL) if DEFAULT_MODEL in list_models else 0
        active_model: str = st.selectbox("Модель генерации", list_models, index=default_idx) or DEFAULT_MODEL
    with cols[1]:
        use_mq = st.checkbox("Multi-query", value=True, help="Переформулировки + RRF")

    for item in st.session_state.chat_history:
        if len(item) == 3:
            q, a, thinking = item
        else:
            q, a = item
            thinking = ""
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            if thinking:
                with st.expander("Процесс размышления", expanded=False):
                    st.markdown(thinking)
            st.markdown(a)

    if user_prompt := st.chat_input("Введите ваш вопрос…"):
        thinking_text = ""
        oai_client = None
        reply = ""
        with st.chat_message("user"):
            st.markdown(user_prompt)
        with st.chat_message("assistant"):
            with st.spinner("Ищу контекст…"):
                try:
                    oai_client, messages, sources_block = prepare_rag_context(
                        embed_collection_name=str(st.session_state.selected_collection),
                        query=user_prompt,
                        model=active_model,
                        top_n=12,
                        db_path=APP_DB_PATH,
                        llm_base_url=OLLAMA_API_URL,
                        embedding_model=None,
                        use_multi_query=bool(use_mq),
                        mq_variants=3,
                        k_per_variant=6,
                        variant_offset=0,
                        exclude_page_ids=[],
                        answers_per_variant=3,
                    )
                except Exception as e:
                    st.error(f"Ошибка: {e}")
                    oai_client = None

            if oai_client is None or messages is None:
                reply = "Я не нашёл релевантный контекст по вашей коллекции."
                st.markdown(reply)
            else:
                thinking_text = ""
                answer_text = ""
                in_think = False

                thinking_expander = st.expander("Процесс размышления", expanded=True)
                thinking_placeholder = thinking_expander.empty()
                answer_placeholder = st.empty()

                stream = oai_client.chat.completions.create(
                    model=active_model,
                    messages=messages,
                    temperature=0,
                    stream=True,
                )

                for chunk in stream:
                    delta = chunk.choices[0].delta

                    # 1) reasoning_content — отдельное поле (DeepSeek / o1 через LiteLLM)
                    reasoning = getattr(delta, "reasoning_content", None) or ""
                    if reasoning:
                        thinking_text += reasoning
                        thinking_placeholder.markdown(thinking_text + "▌")

                    # 2) content — может содержать инлайн <think>...</think>
                    token = delta.content or ""
                    if not token:
                        continue

                    buf = token
                    while buf:
                        if in_think:
                            end_idx = buf.find("</think>")
                            if end_idx != -1:
                                thinking_text += buf[:end_idx]
                                buf = buf[end_idx + len("</think>") :]
                                in_think = False
                                thinking_placeholder.markdown(thinking_text)
                            else:
                                thinking_text += buf
                                buf = ""
                                thinking_placeholder.markdown(thinking_text + "▌")
                        else:
                            start_idx = buf.find("<think>")
                            if start_idx != -1:
                                answer_text += buf[:start_idx]
                                buf = buf[start_idx + len("<think>") :]
                                in_think = True
                            else:
                                answer_text += buf
                                buf = ""
                                if answer_text.strip():
                                    answer_placeholder.markdown(answer_text + "▌")

                if thinking_text.strip():
                    thinking_placeholder.markdown(thinking_text)
                else:
                    thinking_expander.empty()

                if sources_block:
                    answer_text = f"{answer_text}\n\n---\n**Источники:**\n{sources_block}"
                answer_placeholder.markdown(answer_text)
                reply = answer_text

        st.session_state.chat_history.append((user_prompt, reply, thinking_text if oai_client else ""))
        st.session_state.last_prompt_base = user_prompt
        st.session_state.variants[user_prompt] = 0
        used = set(_extract_page_ids_from_answer(reply))
        st.session_state.used_page_ids[user_prompt] = used

    st.caption(
        f"Текущая модель: **{st.session_state.selected_model_name}** · "
        f"Коллекция: **{st.session_state.selected_collection}** · "
        f"Multi-query: {'ON' if use_mq else 'OFF'}"
    )

# ======================== Tab: Data
with tabData:
    st.title("Векторное хранилище")

    db_colls = list_collections()
    if db_colls and st.session_state.selected_collection not in db_colls:
        st.session_state.selected_collection = db_colls[0]

    st.session_state.selected_collection = st.selectbox(
        "Коллекция",
        db_colls or [st.session_state.selected_collection],
        key="select_collection_data",
    )

    show_full = st.toggle("Показывать полный состав коллекции", value=False)
    if show_full:
        with st.spinner("Гружу полный состав…"):
            show_collection_contents(str(st.session_state.selected_collection))
    else:
        st.caption("Превью (сокращено до 200 символов):")
        st.dataframe(
            get_conn_table_preview(str(st.session_state.selected_collection)),
            height=400,
        )

    if st.button(f"Удалить коллекцию «{st.session_state.selected_collection}»", type="primary"):
        try:
            remove_collection(str(st.session_state.selected_collection), db_path=APP_DB_PATH)
            st.success("Коллекция удалена.")
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Не удалось удалить: {e}")

# ======================== Tab: Load (Confluence)
with tabLoad:
    st.title("Загрузка данных из Confluence")

    source_type = st.radio("Источник", ["pageIds", "spaceKey"], horizontal=True, key="classic_src")

    if source_type == "spaceKey":
        st.subheader("Загрузить пространство")
        space_key = st.text_input("Space Key", key="spaceKey_stream")
        col_name = st.text_input("Collection name", key="pCol_stream", value=space_key or "")
        summarize = st.checkbox("Делать резюме", value=False)

        if st.button("Загрузить пространство"):
            if not space_key or not col_name:
                st.warning("Укажите Space Key и имя коллекции.")
            else:
                try:
                    pages, ok_docs, bad_docs = ingest_space_incremental(
                        base_url=KB_URL,
                        token=CONFLUENCE_T,
                        space_key=space_key,
                        collection_name=col_name,
                        db_path=APP_DB_PATH,
                        ollama_api_url=OLLAMA_API_URL,
                        summarize=bool(summarize),
                        verify_ssl=False,
                    )
                    st.success(
                        f"Готово. Обработано страниц: {pages}. "
                        f"Успешно добавлено документов: {ok_docs}, ошибок: {bad_docs}"
                    )
                    st.cache_data.clear()
                except Exception as e:
                    st.error(f"Ошибка загрузки: {e}")

    elif source_type == "pageIds":
        st.subheader("Загрузка страницы")

        pids = st.text_input("Page IDs через запятую", key="pIds")
        col_name2 = st.text_input(
            "Collection name для загрузки",
            key="pColName_classic",
            value=pids.replace(",", "_") if pids else "",
        )

        if st.button("Загрузить страницы"):
            loader = ConfluenceLoader(
                url=KB_URL,
                token=CONFLUENCE_T,
                include_attachments=False,
                keep_markdown_format=True,
                content_format=ContentFormat.EXPORT_VIEW,
                page_ids=[x.strip() for x in pids.split(",") if x.strip()],
                confluence_kwargs={"verify_ssl": False},
                limit=50,
            )
            docs = loader.load()
            st.session_state["confl_docs"] = docs
            st.success(f"Загружено страниц: {len(docs)}")

        if st.button("Сохранить в ChromaDB"):
            docs = st.session_state.get("confl_docs", [])
            if not docs:
                st.warning("Сначала загрузите документы.")
            elif not col_name2:
                st.warning("Укажите имя коллекции.")
            else:
                chunks = split_into_chunks_semantic(docs, ollama_api_url=LITELLM_URL, tokenizer=enc)
                store = store_to_chroma(
                    chunks,
                    collection_name=col_name2,
                    db_path=APP_DB_PATH,
                    batch_size=32,
                    llm_base_url=OLLAMA_API_URL,
                )
                if store is not None:
                    store_data = store.get()
                    st.success(f"Документов в коллекции: {len(store_data['documents'] or [])}")
                st.cache_data.clear()
