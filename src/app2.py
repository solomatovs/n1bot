# v 8 app.py
import os, sys
from pathlib import Path
from typing import List
import streamlit as st
import pandas as pd
import requests

import urllib3
urllib3.disable_warnings()

# sqlite hack для некоторых окружений
try:
    import pysqlite3  # type: ignore
    sys.modules["sqlite3"] = sys.modules["pysqlite3"]
except Exception:
    pass

import chromadb
from chromadb.config import Settings
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat

import n1helper as n1h
import urllib3
urllib3.disable_warnings()
# ========================= Page & Config
st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

APP_DB_PATH = Path(os.environ["CHROMA_DB_PATH"]).as_posix()
LITELLM_URL: str = st.secrets.get("LITELLM_URL")
LITELLM_API_KEY: str = st.secrets.get("LITELLM_API_KEY")

OLLAMA_API_URL=LITELLM_URL
OLLAMA_OPENAI_URL: str = f"{LITELLM_URL.rstrip('/')}/v1"
KB_URL: str = st.secrets.get("CONFLUENCE_URL")
CONFLUENCE_T: str = st.secrets.get("CONFLUENCE_TOKEN")

DEFAULT_COLLECTION : str = st.secrets.get("DEFAULT_COLLECTION")
DEFAULT_MODEL : str = st.secrets.get("LLM_MODEL")

# ========================= Cached resources & helpers
@st.cache_resource(show_spinner=False)
def get_chroma_client() -> chromadb.PersistentClient:
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


def _fetch_collection_df(client: chromadb.PersistentClient, collection_name: str, preview: bool = False) -> pd.DataFrame:
    coll = client.get_collection(collection_name)
    data = coll.get(include=["documents", "metadatas"])
    ids = data.get("ids", [])
    docs = data.get("documents", []) or []
    metas = data.get("metadatas", []) or []
    if preview:
        docs = [(d) if isinstance(d, str) else str(d) for d in docs]
    return pd.DataFrame({"id": ids, "text": docs, "metadata": metas})


def show_collection_contents(collection_name: str):
    client = get_chroma_client()
    try:
        df = _fetch_collection_df(client, collection_name, preview=False)
        st.success(f"📄 Документов: {len(df)} в «{collection_name}»")
        st.dataframe(df, height=500)
    except Exception as e:
        st.error(f"⚠️ Ошибка при получении коллекции: {e}")


@st.cache_data(ttl=20, show_spinner=False)
def get_conn_table_preview(collection_name: str) -> pd.DataFrame:
    try:
        client = get_chroma_client()
        return _fetch_collection_df(client, collection_name, preview=True)
    except Exception as e:
        st.error(f"Ошибка загрузки данных: {e}")
        return pd.DataFrame(columns=["id", "text", "metadata"])


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


import re
def _extract_page_ids_from_answer(ans: str) -> List[str]:
    return re.findall(r"-\s+[^\s:]+:(\d+)\b", ans)


# ======================== UI Tabs
tabChat, tabLoad, tabData = st.tabs(["Чат", "Загрузка из Confluence", "Векторное хранилище"])

# ======================== Tab: Chat

def get_openai_models():
    """Получение списка моделей OpenAI через API"""
    try:
        response = requests.get(LITELLM_URL+"/v1/models",
            headers={"Authorization": f"Bearer {LITELLM_API_KEY}"}
        )
        response.raise_for_status()
        models = [model['id'] for model in response.json()['data']]
        return sorted(models)
    except Exception as e:
        st.error(f"Ошибка получения моделей: {e}")
        return []
        
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
        list_models=get_openai_models()
        default_idx = list_models.index(DEFAULT_MODEL) if DEFAULT_MODEL in list_models else 0
        selected_model_name=st.selectbox("Модель генерации",list_models, index=default_idx)
        DEFAULT_MODEL=selected_model_name
    with cols[1]:
        use_mq = st.checkbox("Multi-query", value=True, help="Переформулировки + RRF")
    # with cols[2]:
    #     if st.button("🔄 Обновить модели"):
    #         st.session_state.available_models = get_available_models(OLLAMA_API_URL)
    #         st.rerun()

    for q, a in st.session_state.chat_history:
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            st.markdown(a)

    if user_prompt := st.chat_input("Введите ваш вопрос…"):
        with st.chat_message("user"):
            st.markdown(user_prompt)
        with st.chat_message("assistant"):
            with st.spinner("Думаю…"):
                reply = n1h.generate_answer_with_context(
                    embed_collection_name=st.session_state.selected_collection,
                    query=user_prompt,
                    # model=st.session_state.selected_model_name,
                    model=DEFAULT_MODEL,
                    top_n=12,
                    db_path=APP_DB_PATH,
                    llm_base_url=OLLAMA_API_URL,  # Используем base URL без /v1
                    embedding_model=None,
                    use_multi_query=bool(use_mq),
                    mq_variants=3,
                    k_per_variant=6,
                    variant_offset=0,
                    exclude_page_ids=[],
                    answers_per_variant=3,
                )
                st.markdown(reply)

        st.session_state.chat_history.append((user_prompt, reply))
        st.session_state.last_prompt_base = user_prompt
        st.session_state.variants[user_prompt] = 0
        used = set(_extract_page_ids_from_answer(reply))
        st.session_state.used_page_ids[user_prompt] = used

    # if st.session_state.last_prompt_base:
    #     if st.button("Ещё вариант"):
    #         base = st.session_state.last_prompt_base
    #         st.session_state.variants[base] = st.session_state.variants.get(base, 0) + 1
    #         exclude = list(st.session_state.used_page_ids.get(base, set()))

    #         with st.chat_message("assistant"):
    #             with st.spinner("Подбираю другой вариант…"):
    #                 reply2 = n1h.generate_answer_with_context(
    #                     embed_collection_name=st.session_state.selected_collection,
    #                     query=base,
    #                     model=st.session_state.selected_model_name,
    #                     top_n=12,
    #                     db_path=APP_DB_PATH,
    #                     llm_base_url=OLLAMA_API_URL,  # Изменено
    #                     embedding_model=None,
    #                     use_multi_query=bool(use_mq),
    #                     mq_variants=3,
    #                     k_per_variant=6,
    #                     variant_offset=st.session_state.variants[base],
    #                     exclude_page_ids=exclude,
    #                     answers_per_variant=3,
    #                 )
    #                 st.markdown(reply2)

    #         st.session_state.chat_history.append((f"{base} (ещё вариант)", reply2))
    #         new_ids = set(_extract_page_ids_from_answer(reply2))
    #         st.session_state.used_page_ids.setdefault(base, set()).update(new_ids)

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
        "Коллекция", db_colls or [st.session_state.selected_collection], key="select_collection_data"
    )

    show_full = st.toggle("Показывать полный состав коллекции", value=False)
    if show_full:
        with st.spinner("Гружу полный состав…"):
            show_collection_contents(st.session_state.selected_collection)
    else:
        st.caption("Превью (сокращено до 200 символов):")
        st.dataframe(
            get_conn_table_preview(st.session_state.selected_collection),
            height=400,
        )

    if st.button(f"Удалить коллекцию «{st.session_state.selected_collection}»", type="primary"):
        try:
            n1h.remove_collection(st.session_state.selected_collection, db_path=APP_DB_PATH)
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
                    pages, ok_docs, bad_docs = n1h.ingest_space_incremental(
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
                    with open("log.txt", "w") as file:
                        file.write(f"Готово. Обработано страниц: {pages}. "
                        f"Успешно добавлено документов: {ok_docs}, ошибок: {bad_docs}")
                    st.cache_data.clear()
                except Exception as e:
                    st.error(f"Ошибка загрузки: {e}")

    elif source_type == "pageIds":
        st.subheader("Загрузка страницы")

        pids = st.text_input("Page IDs через запятую", key="pIds")
        col_name2 = st.text_input(
            "Collection name для загрузки", key="pColName_classic", value=pids.replace(",", "_") if pids else ""
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
                chunks = n1h.split_into_chunks_semantic(
                    # docs, ollama_api_url=OLLAMA_API_URL, tokenizer=n1h.enc
                    docs, ollama_api_url=LITELLM_URL, tokenizer=n1h.enc
                )
                store = n1h.store2Chroma(
                    chunks,
                    collection_name=col_name2,
                    db_path=APP_DB_PATH,
                    batch_size=32,
                    # ollama_api_url=OLLAMA_API_URL,
                    llm_base_url=OLLAMA_API_URL,
                )
                st.success(f"📦 Документов в коллекции: {len(store.get()['documents'])}")
                st.cache_data.clear()



# # v 8 app.py
# import os, sys
# from pathlib import Path
# from typing import List
# import streamlit as st
# import pandas as pd
# import requests

# import urllib3
# urllib3.disable_warnings()

# # sqlite hack для некоторых окружений
# try:
#     import pysqlite3  # type: ignore
#     sys.modules["sqlite3"] = sys.modules["pysqlite3"]
# except Exception:
#     pass

# import chromadb
# from chromadb.config import Settings
# from langchain_community.document_loaders import ConfluenceLoader
# from langchain_community.document_loaders.confluence import ContentFormat

# import n1helper as n1h
# import urllib3
# urllib3.disable_warnings()
# # ========================= Page & Config
# st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

# APP_DB_PATH = Path(os.getenv("CHROMA_DB_PATH", "./chroma_app_db")).as_posix()
# # OLLAMA_API_URL: str = st.secrets.get("OLLAMA_API_URL", os.getenv("OLLAMA_API_URL", "https://spb99akl-dgx02.gazprom-neft.local"))
# LITELLM_URL=  "https://spb99akl-dgx02.gazprom-neft.local"
# OLLAMA_API_URL=LITELLM_URL
# OLLAMA_OPENAI_URL: str = f"{LITELLM_URL.rstrip('/')}/v1"
# KB_URL: str = st.secrets.get("CONFLUENCE_URL", os.getenv("CONFLUENCE_URL", "https://kb.gazprom-neft.local"))
# CONFLUENCE_T: str = st.secrets.get("CONFLUENCE_TOKEN", os.getenv("CONFLUENCE_TOKEN", ""))

# DEFAULT_COLLECTION = os.getenv("DEFAULT_COLLECTION", "testing")
# DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen3:latest")

# # ========================= Cached resources & helpers
# @st.cache_resource(show_spinner=False)
# def get_chroma_client() -> chromadb.PersistentClient:
#     return chromadb.PersistentClient(path=APP_DB_PATH, settings=Settings(anonymized_telemetry=False))


# @st.cache_data(ttl=60, show_spinner=False)
# def list_collections() -> List[str]:
#     try:
#         return [c.name for c in get_chroma_client().list_collections()]
#     except Exception as ex:
#         st.warning(f"Не удалось получить список коллекций: {ex}")
#         return []


# @st.cache_data(ttl=60, show_spinner=False)
# def get_available_models(ollama_url: str) -> List[str]:
#     try:
#         r = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=10)
#         if r.ok:
#             data = r.json() or {}
#             names = [m.get("name") for m in data.get("models", [])]
#             return names or [DEFAULT_MODEL]
#     except Exception:
#         pass
#     return [DEFAULT_MODEL]


# def _fetch_collection_df(client: chromadb.PersistentClient, collection_name: str, preview: bool = False) -> pd.DataFrame:
#     coll = client.get_collection(collection_name)
#     data = coll.get(include=["documents", "metadatas"])
#     ids = data.get("ids", [])
#     docs = data.get("documents", []) or []
#     metas = data.get("metadatas", []) or []
#     if preview:
#         docs = [(d) if isinstance(d, str) else str(d) for d in docs]
#     return pd.DataFrame({"id": ids, "text": docs, "metadata": metas})


# def show_collection_contents(collection_name: str):
#     client = get_chroma_client()
#     try:
#         df = _fetch_collection_df(client, collection_name, preview=False)
#         st.success(f"📄 Документов: {len(df)} в «{collection_name}»")
#         st.dataframe(df, height=500)
#     except Exception as e:
#         st.error(f"⚠️ Ошибка при получении коллекции: {e}")


# @st.cache_data(ttl=20, show_spinner=False)
# def get_conn_table_preview(collection_name: str) -> pd.DataFrame:
#     try:
#         client = get_chroma_client()
#         return _fetch_collection_df(client, collection_name, preview=True)
#     except Exception as e:
#         st.error(f"Ошибка загрузки данных: {e}")
#         return pd.DataFrame(columns=["id", "text", "metadata"])


# # ========================= Session defaults
# if "selected_collection" not in st.session_state:
#     st.session_state.selected_collection = DEFAULT_COLLECTION

# if "selected_model_name" not in st.session_state:
#     st.session_state.selected_model_name = DEFAULT_MODEL

# if "chat_history" not in st.session_state:
#     st.session_state.chat_history = []

# if "available_models" not in st.session_state:
#     st.session_state.available_models = get_available_models(OLLAMA_API_URL)

# if "last_prompt_base" not in st.session_state:
#     st.session_state.last_prompt_base = ""
# if "variants" not in st.session_state:
#     st.session_state.variants = {}
# if "used_page_ids" not in st.session_state:
#     st.session_state.used_page_ids = {}


# import re
# def _extract_page_ids_from_answer(ans: str) -> List[str]:
#     return re.findall(r"-\s+[^\s:]+:(\d+)\b", ans)


# # ======================== UI Tabs
# tabChat, tabLoad, tabData = st.tabs(["Чат", "Загрузка из Confluence", "Векторное хранилище"])

# # ======================== Tab: Chat

# def get_openai_models():
#     """Получение списка моделей OpenAI через API"""
#     try:
#         response = requests.get(LITELLM_URL+"/v1/models",
#             headers={"Authorization": f"Bearer sk-fe1ZWrr7lPUN7tb8ZFlYEw"}
#         )
#         response.raise_for_status()
#         models = [model['id'] for model in response.json()['data']]
#         return sorted(models)
#     except Exception as e:
#         st.error(f"Ошибка получения моделей: {e}")
#         return []
        
# with tabChat:
#     st.title("N1 Hub AI bots")

#     db_colls = list_collections()
#     st.session_state.selected_collection = st.selectbox(
#         "Имя векторной БД (коллекция)",
#         db_colls or [st.session_state.selected_collection],
#         index=(
#             db_colls.index(st.session_state.selected_collection)
#             if st.session_state.selected_collection in db_colls
#             else 0
#         ),
#         key="select_collection_chat",
#     )

#     cols = st.columns([3, 1, 1])
#     with cols[0]:
#         list_models=get_openai_models()
#         selected_model_name=st.selectbox("Модель генерации",list_models)
#         # st.session_state.selected_model_name = st.selectbox(
#         #     "Модель генерации",
#         #     st.session_state.available_models,
#         #     index=(
#         #         st.session_state.available_models.index(st.session_state.selected_model_name)
#         #         if st.session_state.selected_model_name in st.session_state.available_models
#         #         else 0
#         #     ),
#         #     key="select_model_name",
#         # )
#     with cols[1]:
#         use_mq = st.checkbox("Multi-query", value=True, help="Переформулировки + RRF")
#     with cols[2]:
#         if st.button("🔄 Обновить модели"):
#             st.session_state.available_models = get_available_models(OLLAMA_API_URL)
#             st.rerun()

#     for q, a in st.session_state.chat_history:
#         with st.chat_message("user"):
#             st.markdown(q)
#         with st.chat_message("assistant"):
#             st.markdown(a)

#     if user_prompt := st.chat_input("Введите ваш вопрос…"):
#         with st.chat_message("user"):
#             st.markdown(user_prompt)
#         with st.chat_message("assistant"):
#             with st.spinner("Думаю…"):
#                 reply = n1h.generate_answer_with_context(
#                     embed_collection_name=st.session_state.selected_collection,
#                     query=user_prompt,
#                     model=st.session_state.selected_model_name,
#                     top_n=12,
#                     db_path=APP_DB_PATH,
#                     llm_base_url=OLLAMA_API_URL,  # Используем base URL без /v1
#                     embedding_model=None,
#                     use_multi_query=bool(use_mq),
#                     mq_variants=3,
#                     k_per_variant=6,
#                     variant_offset=0,
#                     exclude_page_ids=[],
#                     answers_per_variant=3,
#                 )
#                 st.markdown(reply)

#         st.session_state.chat_history.append((user_prompt, reply))
#         st.session_state.last_prompt_base = user_prompt
#         st.session_state.variants[user_prompt] = 0
#         used = set(_extract_page_ids_from_answer(reply))
#         st.session_state.used_page_ids[user_prompt] = used

#     if st.session_state.last_prompt_base:
#         if st.button("Ещё вариант"):
#             base = st.session_state.last_prompt_base
#             st.session_state.variants[base] = st.session_state.variants.get(base, 0) + 1
#             exclude = list(st.session_state.used_page_ids.get(base, set()))

#             with st.chat_message("assistant"):
#                 with st.spinner("Подбираю другой вариант…"):
#                     reply2 = n1h.generate_answer_with_context(
#                         embed_collection_name=st.session_state.selected_collection,
#                         query=base,
#                         model=st.session_state.selected_model_name,
#                         top_n=12,
#                         db_path=APP_DB_PATH,
#                         llm_base_url=OLLAMA_API_URL,  # Изменено
#                         embedding_model=None,
#                         use_multi_query=bool(use_mq),
#                         mq_variants=3,
#                         k_per_variant=6,
#                         variant_offset=st.session_state.variants[base],
#                         exclude_page_ids=exclude,
#                         answers_per_variant=3,
#                     )
#                     st.markdown(reply2)

#             st.session_state.chat_history.append((f"{base} (ещё вариант)", reply2))
#             new_ids = set(_extract_page_ids_from_answer(reply2))
#             st.session_state.used_page_ids.setdefault(base, set()).update(new_ids)

#     st.caption(
#         f"Текущая модель: **{st.session_state.selected_model_name}** · "
#         f"Коллекция: **{st.session_state.selected_collection}** · "
#         f"Multi-query: {'ON' if use_mq else 'OFF'}"
#     )

# # ======================== Tab: Data
# with tabData:
#     st.title("Векторное хранилище")

#     db_colls = list_collections()
#     if db_colls and st.session_state.selected_collection not in db_colls:
#         st.session_state.selected_collection = db_colls[0]

#     st.session_state.selected_collection = st.selectbox(
#         "Коллекция", db_colls or [st.session_state.selected_collection], key="select_collection_data"
#     )

#     show_full = st.toggle("Показывать полный состав коллекции", value=False)
#     if show_full:
#         with st.spinner("Гружу полный состав…"):
#             show_collection_contents(st.session_state.selected_collection)
#     else:
#         st.caption("Превью (сокращено до 200 символов):")
#         st.dataframe(
#             get_conn_table_preview(st.session_state.selected_collection),
#             height=400,
#         )

#     if st.button(f"Удалить коллекцию «{st.session_state.selected_collection}»", type="primary"):
#         try:
#             n1h.remove_collection(st.session_state.selected_collection, db_path=APP_DB_PATH)
#             st.success("Коллекция удалена.")
#             st.cache_data.clear()
#             st.cache_resource.clear()
#             st.rerun()
#         except Exception as e:
#             st.error(f"Не удалось удалить: {e}")

# # ======================== Tab: Load (Confluence)
# with tabLoad:
#     st.title("Загрузка данных из Confluence")

#     source_type = st.radio("Источник", ["pageIds", "spaceKey"], horizontal=True, key="classic_src")

#     if source_type == "spaceKey":
#         st.subheader("Загрузить пространство")
#         space_key = st.text_input("Space Key", key="spaceKey_stream")
#         col_name = st.text_input("Collection name", key="pCol_stream", value=space_key or "")

#         summarize = st.checkbox("Делать резюме", value=False)

#         if st.button("Загрузить пространство"):
#             if not space_key or not col_name:
#                 st.warning("Укажите Space Key и имя коллекции.")
#             else:
#                 try:
#                     pages, ok_docs, bad_docs = n1h.ingest_space_incremental(
#                         base_url=KB_URL,
#                         token=CONFLUENCE_T,
#                         space_key=space_key,
#                         collection_name=col_name,
#                         db_path=APP_DB_PATH,
#                         ollama_api_url=OLLAMA_API_URL,
#                         summarize=bool(summarize),
#                         verify_ssl=False,
#                     )
#                     st.success(
#                         f"Готово. Обработано страниц: {pages}. "
#                         f"Успешно добавлено документов: {ok_docs}, ошибок: {bad_docs}"
#                     )
#                     with open("log.txt", "w") as file:
#                         file.write(f"Готово. Обработано страниц: {pages}. "
#                         f"Успешно добавлено документов: {ok_docs}, ошибок: {bad_docs}")
#                     st.cache_data.clear()
#                 except Exception as e:
#                     st.error(f"Ошибка загрузки: {e}")

#     elif source_type == "pageIds":
#         st.subheader("Загрузка страницы")

#         pids = st.text_input("Page IDs через запятую", key="pIds")
#         col_name2 = st.text_input(
#             "Collection name для загрузки", key="pColName_classic", value=pids.replace(",", "_") if pids else ""
#         )

#         if st.button("Загрузить страницы"):
#             loader = ConfluenceLoader(
#                 url=KB_URL,
#                 token=CONFLUENCE_T,
#                 include_attachments=False,
#                 keep_markdown_format=True,
#                 content_format=ContentFormat.EXPORT_VIEW,
#                 page_ids=[x.strip() for x in pids.split(",") if x.strip()],
#                 confluence_kwargs={"verify_ssl": False},
#                 limit=50,
#             )
#             docs = loader.load()
#             st.session_state["confl_docs"] = docs
#             st.success(f"Загружено страниц: {len(docs)}")

#         if st.button("Сохранить в ChromaDB"):
#             docs = st.session_state.get("confl_docs", [])
#             if not docs:
#                 st.warning("Сначала загрузите документы.")
#             elif not col_name2:
#                 st.warning("Укажите имя коллекции.")
#             else:
#                 chunks = n1h.split_into_chunks_semantic(
#                     # docs, ollama_api_url=OLLAMA_API_URL, tokenizer=n1h.enc
#                     docs, ollama_api_url=LITELLM_URL, tokenizer=n1h.enc
#                 )
#                 store = n1h.store2Chroma(
#                     chunks,
#                     collection_name=col_name2,
#                     db_path=APP_DB_PATH,
#                     batch_size=32,
#                     # ollama_api_url=OLLAMA_API_URL,
#                     llm_base_url=OLLAMA_API_URL,
#                 )
#                 # st.success(f"📦 Документов в коллекции: {len(store.get()['documents'])}")
#                 st.cache_data.clear()

