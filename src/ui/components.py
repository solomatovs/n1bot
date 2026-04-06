"""Переиспользуемые UI-компоненты Streamlit."""
from __future__ import annotations

import logging
import re
from typing import List

import pandas as pd
import requests
import streamlit as st

import chromadb
import chromadb.errors
from chromadb.config import Settings
from streamlit.delta_generator import DeltaGenerator

from ui.state import (
    AppConfig,
    ChatMessage,
    ChunkingParams,
    ContentType,
    PromptParams,
    SearchParams,
    SpaceLoadParams,
    StorageParams,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Кэшируемые ресурсы
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def get_chroma_client(db_path: str):  # -> chromadb.PersistentClient
    return chromadb.PersistentClient(
        path=db_path,
        settings=Settings(anonymized_telemetry=False),
    )


@st.cache_data(ttl=60, show_spinner=False)
def list_collections(db_path: str) -> List[str]:
    try:
        return [c.name for c in get_chroma_client(db_path).list_collections()]
    except (chromadb.errors.ChromaError, ValueError, OSError) as ex:
        log.warning("Failed to list collections: %s", ex)
        st.warning(f"Не удалось получить список коллекций: {ex}")
        return []


@st.cache_data(ttl=60, show_spinner=False)
def get_openai_models(openai_url: str, api_key: str) -> List[str]:
    try:
        resp = requests.get(
            f"{openai_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return sorted(m["id"] for m in resp.json()["data"])
    except (requests.RequestException, KeyError, ValueError) as e:
        log.warning("Failed to fetch models: %s", e)
        st.error(f"Ошибка получения моделей: {e}")
        return []


# ---------------------------------------------------------------------------
# Помощники для коллекций
# ---------------------------------------------------------------------------

def fetch_collection_df(
    db_path: str,
    collection_name: str,
    preview: bool = False,
) -> pd.DataFrame:
    coll = get_chroma_client(db_path).get_collection(collection_name)
    data = coll.get(include=["documents", "metadatas"])
    ids = data.get("ids", [])
    docs = data.get("documents", []) or []
    metas = data.get("metadatas", []) or []
    if preview:
        docs = [d if isinstance(d, str) else str(d) for d in docs]
    return pd.DataFrame({"id": ids, "text": docs, "metadata": metas})


@st.cache_data(ttl=20, show_spinner=False)
def get_collection_preview(db_path: str, collection_name: str) -> pd.DataFrame:
    try:
        return fetch_collection_df(db_path, collection_name, preview=True)
    except (chromadb.errors.ChromaError, ValueError, OSError) as e:
        log.warning("Failed to load collection data: %s", e)
        st.error(f"Ошибка загрузки данных: {e}")
        return pd.DataFrame({"id": [], "text": [], "metadata": []})


# ---------------------------------------------------------------------------
# Селекторы
# ---------------------------------------------------------------------------

def collection_selector(cfg: AppConfig, *, key: str, current: str) -> str:
    """Отрисовать селектор коллекции и вернуть выбранное значение."""
    colls = list_collections(cfg.chroma_db_path)
    index = colls.index(current) if current in colls else 0
    return st.selectbox(
        "Имя векторной БД (коллекция)",
        colls or [current],
        index=index,
        key=key,
    ) or current


def model_selector(cfg: AppConfig) -> str:
    """Отрисовать селектор модели и вернуть выбранное значение."""
    models = get_openai_models(cfg.openai_url, cfg.litellm_api_key)
    default_idx = models.index(cfg.default_model) if cfg.default_model in models else 0
    return st.selectbox("Модель генерации", models, index=default_idx) or cfg.default_model


# ---------------------------------------------------------------------------
# Рендер истории чата
# ---------------------------------------------------------------------------

def render_chat_history(history: List[ChatMessage]) -> None:
    """Отрисовать историю чата с раскрывающимися блоками контекста и размышлений."""
    for msg in history:
        with st.chat_message("user"):
            st.markdown(msg.question)
        with st.chat_message("assistant"):
            if msg.rag_context:
                with st.expander("Найденный контекст из базы знаний", expanded=False):
                    st.markdown(msg.rag_context)
            if msg.thinking:
                with st.expander("Процесс размышления", expanded=False):
                    st.markdown(msg.thinking)
            st.markdown(msg.answer)


# ---------------------------------------------------------------------------
# Настройки поиска
# ---------------------------------------------------------------------------

def render_search_settings(container: DeltaGenerator) -> SearchParams:
    """Отрисовать панель настроек поиска в popover и вернуть SearchParams."""
    defaults = SearchParams()

    with container.popover("Настройки поиска", use_container_width=True):

        # -- Группа: Поиск по векторной базе --
        st.markdown("##### Поиск")
        col1, col2 = st.columns(2)
        with col1:
            top_n = st.slider(
                "Глубина поиска",
                min_value=1, max_value=30, value=defaults.top_n,
                help="Сколько кандидатов извлекать из векторной базы",
                key="sp_top_n",
            )
            per_page = st.slider(
                "Чанков с одной страницы",
                min_value=1, max_value=5, value=defaults.per_page,
                help="Максимум чанков с одной Confluence-страницы (дедупликация)",
                key="sp_per_page",
            )
        with col2:
            answers = st.slider(
                "Документов в контекст",
                min_value=1, max_value=10, value=defaults.answers_per_variant,
                help="Сколько финальных чанков отдать модели для генерации ответа",
                key="sp_answers",
            )

        chosen_labels = st.multiselect(
            "Типы контента",
            options=[ct.label for ct in ContentType],
            default=[],
            help="Пусто = автоопределение по запросу. Иначе поиск только по выбранным типам",
            key="sp_content_types",
        )
        label_to_ct = {ct.label: ct.key for ct in ContentType}
        content_types = [label_to_ct[lb] for lb in chosen_labels] or None

        st.divider()

        # -- Группа: Multi-query --
        st.markdown("##### Multi-query")
        use_mq = st.checkbox(
            "Включить переформулировки + RRF",
            value=defaults.use_multi_query,
            key="sp_use_mq",
        )
        col3, col4 = st.columns(2)
        with col3:
            mq_variants = st.slider(
                "Переформулировок",
                min_value=1, max_value=5, value=defaults.mq_variants,
                disabled=not use_mq,
                help="Количество вариантов запроса для multi-query",
                key="sp_mq_variants",
            )
        with col4:
            k_per_variant = st.slider(
                "Документов на вариант",
                min_value=1, max_value=15, value=defaults.k_per_variant,
                disabled=not use_mq,
                help="Сколько документов извлекать на каждую переформулировку",
                key="sp_k_per_var",
            )

        st.divider()

        # -- Группа: Генерация --
        st.markdown("##### Генерация")
        col_t, col_tp = st.columns(2)
        with col_t:
            temperature = st.slider(
                "Температура",
                min_value=0.0, max_value=2.0, value=defaults.temperature, step=0.05,
                help="0 = детерминированный, 2 = максимальная креативность",
                key="sp_temperature",
            )
        with col_tp:
            top_p = st.slider(
                "Top-p (nucleus)",
                min_value=0.0, max_value=1.0, value=defaults.top_p, step=0.05,
                help="Отсекает маловероятные токены. 1.0 = без ограничений",
                key="sp_top_p",
            )

        use_max_tokens = st.checkbox(
            "Ограничить длину ответа",
            value=defaults.max_tokens is not None,
            key="sp_use_max_tokens",
        )
        max_tokens: int | None = None
        if use_max_tokens:
            max_tokens = st.slider(
                "Макс. токенов",
                min_value=64, max_value=4096, value=defaults.max_tokens or 1024, step=64,
                help="Максимальная длина ответа модели в токенах",
                key="sp_max_tokens",
            )

        col_fp, col_pp = st.columns(2)
        with col_fp:
            frequency_penalty = st.slider(
                "Frequency penalty",
                min_value=-2.0, max_value=2.0, value=defaults.frequency_penalty, step=0.1,
                help="Штраф за повторение слов. >0 = меньше повторов",
                key="sp_freq_penalty",
            )
        with col_pp:
            presence_penalty = st.slider(
                "Presence penalty",
                min_value=-2.0, max_value=2.0, value=defaults.presence_penalty, step=0.1,
                help="Штраф за повторение тем. >0 = больше разнообразия",
                key="sp_pres_penalty",
            )

    return SearchParams(
        top_n=top_n,
        answers_per_variant=answers,
        per_page=per_page,
        content_types=content_types,
        use_multi_query=use_mq,
        mq_variants=mq_variants,
        k_per_variant=k_per_variant,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
    )


# ---------------------------------------------------------------------------
# Настройки промптов
# ---------------------------------------------------------------------------

def render_prompt_settings(container: DeltaGenerator) -> PromptParams:
    """Отрисовать панель настроек промптов в popover и вернуть PromptParams."""
    defaults = PromptParams()

    with container.popover("Промпты", use_container_width=True):
        system_prompt = st.text_area(
            "Системный промпт",
            value=defaults.system_prompt,
            height=100,
            help="Инструкция для модели — задаёт роль и ограничения",
            key="pp_system",
        )
        user_template = st.text_area(
            "Шаблон пользовательского сообщения",
            value=defaults.user_template,
            height=150,
            help="Плейсхолдеры: {context} — текст из базы знаний, {query} — вопрос",
            key="pp_user",
        )

    return PromptParams(
        system_prompt=system_prompt,
        user_template=user_template,
    )


# ---------------------------------------------------------------------------
# Настройки чанкинга
# ---------------------------------------------------------------------------

def render_chunking_settings(key_prefix: str = "cp") -> ChunkingParams:
    """Отрисовать настройки чанкинга и вернуть ChunkingParams."""
    defaults = ChunkingParams()

    with st.expander("Настройки чанкинга", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            max_tokens = st.slider(
                "Макс. токенов на чанк",
                min_value=100, max_value=2000, value=defaults.max_tokens, step=50,
                help="Максимальный размер одного чанка в токенах",
                key=f"{key_prefix}_max_tokens",
            )
        with col2:
            similarity = st.slider(
                "Порог схожести",
                min_value=0.0, max_value=1.0, value=defaults.similarity_threshold, step=0.05,
                help="Параграфы со схожестью выше порога объединяются в один чанк",
                key=f"{key_prefix}_similarity",
            )

    return ChunkingParams(
        max_tokens=max_tokens,
        similarity_threshold=similarity,
    )


# ---------------------------------------------------------------------------
# Настройки загрузки из Confluence (раздельные для pageIds и spaceKey)
# ---------------------------------------------------------------------------

BATCH_SIZE_OPTIONS = [8, 16, 32, 64, 128]


def _render_storage_params(key_prefix: str) -> StorageParams:
    """Параметры сохранения в ChromaDB."""
    defaults = StorageParams()
    batch_size = st.selectbox(
        "Размер батча для ChromaDB",
        options=BATCH_SIZE_OPTIONS,
        index=BATCH_SIZE_OPTIONS.index(defaults.batch_size),
        help="Количество документов, сохраняемых за одну операцию. Меньше = надёжнее, больше = быстрее",
        key=f"{key_prefix}_batch_size",
    )
    return StorageParams(batch_size=batch_size or defaults.batch_size)


def render_page_id_settings() -> StorageParams:
    """Настройки для режима загрузки по Page IDs."""
    with st.expander("Настройки загрузки", expanded=False):
        storage_params = _render_storage_params("pid")
    return storage_params


def render_space_settings() -> tuple[SpaceLoadParams, StorageParams]:
    """Настройки для режима загрузки пространства."""
    defaults = SpaceLoadParams()

    with st.expander("Настройки загрузки", expanded=False):
        st.markdown("##### Пространство")
        col1, col2 = st.columns(2)
        with col1:
            api_page_limit = st.slider(
                "Страниц на запрос API",
                min_value=1, max_value=200, value=defaults.api_page_limit, step=10,
                help="Размер страницы при пагинации Confluence REST API",
                key="sp_api_page_limit",
            )
        with col2:
            use_max_pages = st.checkbox(
                "Ограничить количество страниц",
                value=defaults.max_pages is not None,
                key="sp_use_max_pages",
            )
            max_pages: int | None = None
            if use_max_pages:
                max_pages = st.number_input(
                    "Макс. страниц",
                    min_value=1, value=defaults.max_pages or 100,
                    key="sp_max_pages",
                )

        st.divider()
        storage_params = _render_storage_params("sp")

    space_params = SpaceLoadParams(api_page_limit=api_page_limit, max_pages=max_pages)
    return space_params, storage_params


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def extract_page_ids_from_answer(text: str) -> set[str]:
    return set(re.findall(r"-\s+[^\s:]+:(\d+)\b", text))
