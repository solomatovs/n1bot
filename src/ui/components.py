"""Переиспользуемые UI-компоненты Streamlit."""
from __future__ import annotations

import logging
from typing import List

import pandas as pd
import requests
import streamlit as st

import chromadb
import chromadb.errors
from chromadb.config import Settings
from streamlit.delta_generator import DeltaGenerator

from ui.state import (
    BATCH_SIZE_OPTIONS,
    AppConfig,
    CacheTTL,
    ChatMessage,
    ChunkingLimits,
    ChunkingParams,
    ContentType,
    PromptLimits,
    PromptParams,
    SearchLimits,
    SearchParams,
    SpaceLoadLimits,
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


@st.cache_data(ttl=CacheTTL.collections, show_spinner=False)
def list_collections(db_path: str) -> List[str]:
    try:
        return [c.name for c in get_chroma_client(db_path).list_collections()]
    except (chromadb.errors.ChromaError, ValueError, OSError) as ex:
        log.warning("Failed to list collections: %s", ex)
        st.warning(f"Не удалось получить список коллекций: {ex}")
        return []


@st.cache_data(ttl=CacheTTL.models, show_spinner=False)
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


@st.cache_data(ttl=CacheTTL.preview, show_spinner=False)
def get_collection_preview(db_path: str, collection_name: str) -> pd.DataFrame:
    try:
        return fetch_collection_df(db_path, collection_name, preview=True)
    except (chromadb.errors.ChromaError, ValueError, OSError) as e:
        log.warning("Failed to load collection data: %s", e)
        st.error(f"Ошибка загрузки данных: {e}")
        return pd.DataFrame({"id": [], "text": [], "metadata": []})


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _safe_index(items: List[str], value: str, fallback: int = 0) -> int:
    """Найти индекс значения в списке или вернуть fallback."""
    try:
        return items.index(value)
    except ValueError:
        return fallback


# ---------------------------------------------------------------------------
# Селекторы
# ---------------------------------------------------------------------------

def collection_selector(cfg: AppConfig, *, key: str, current: str) -> str:
    """Отрисовать селектор коллекции и вернуть выбранное значение."""
    colls = list_collections(cfg.chroma_db_path)
    index = _safe_index(colls, current)
    return st.selectbox(
        "Имя векторной БД (коллекция)",
        colls or [current],
        index=index,
        key=key,
    ) or current


def model_selector(cfg: AppConfig) -> str:
    """Отрисовать селектор модели генерации и вернуть выбранное значение."""
    models = get_openai_models(cfg.openai_url, cfg.litellm_api_key)
    return st.selectbox("Модель генерации", models, index=_safe_index(models, cfg.default_model)) or cfg.default_model


def embedding_model_selector(cfg: AppConfig, *, key: str = "embedding_model") -> str:
    """Отрисовать селектор embedding модели и вернуть выбранное значение."""
    models = get_openai_models(cfg.openai_url, cfg.litellm_api_key)
    return st.selectbox("Embedding модель", models, index=_safe_index(models, cfg.embedding_model), key=key) or cfg.embedding_model


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

def render_retrieval_settings(container: DeltaGenerator, key_prefix: str = "sp") -> SearchParams:
    """Отрисовать настройки retrieval (поиск + multi-query) и вернуть SearchParams."""
    defaults = SearchParams()
    lim = SearchLimits
    p = key_prefix

    with container.popover("Настройки поиска", use_container_width=True):

        # -- Группа: Поиск по векторной базе --
        st.markdown("##### Поиск")
        col1, col2 = st.columns(2)
        with col1:
            top_n = st.slider(
                "Глубина поиска",
                min_value=lim.top_n.min, max_value=lim.top_n.max,
                value=defaults.top_n,
                help="Сколько кандидатов извлекать из векторной базы",
                key=f"{p}_top_n",
            )
            per_page = st.slider(
                "Чанков с одной страницы",
                min_value=lim.per_page.min, max_value=lim.per_page.max,
                value=defaults.per_page,
                help="Максимум чанков с одной Confluence-страницы (дедупликация)",
                key=f"{p}_per_page",
            )
        with col2:
            answers = st.slider(
                "Документов в контекст",
                min_value=lim.answers_per_variant.min, max_value=lim.answers_per_variant.max,
                value=defaults.answers_per_variant,
                help="Сколько финальных чанков отдать модели для генерации ответа",
                key=f"{p}_answers",
            )

        chosen_labels = st.multiselect(
            "Типы контента",
            options=[ct.label for ct in ContentType],
            default=[],
            help="Пусто = автоопределение по запросу. Иначе поиск только по выбранным типам",
            key=f"{p}_content_types",
        )
        content_types = ContentType.labels_to_keys(chosen_labels)

        st.divider()

        # -- Группа: Multi-query --
        st.markdown("##### Multi-query")
        use_mq = st.checkbox(
            "Включить переформулировки + RRF",
            value=defaults.use_multi_query,
            key=f"{p}_use_mq",
        )
        col3, col4 = st.columns(2)
        with col3:
            mq_variants = st.slider(
                "Переформулировок",
                min_value=lim.mq_variants.min, max_value=lim.mq_variants.max,
                value=defaults.mq_variants,
                disabled=not use_mq,
                help="Количество вариантов запроса для multi-query",
                key=f"{p}_mq_variants",
            )
        with col4:
            k_per_variant = st.slider(
                "Документов на вариант",
                min_value=lim.k_per_variant.min, max_value=lim.k_per_variant.max,
                value=defaults.k_per_variant,
                disabled=not use_mq,
                help="Сколько документов извлекать на каждую переформулировку",
                key=f"{p}_k_per_var",
            )
        mq_prompt_template = st.text_area(
            "Промпт переформулировки",
            value=defaults.mq_prompt_template,
            disabled=not use_mq,
            help="Плейсхолдеры: {n} — количество, {query} — запрос пользователя",
            key=f"{p}_mq_prompt",
        )

    return SearchParams(
        top_n=top_n,
        answers_per_variant=answers,
        per_page=per_page,
        content_types=content_types,
        use_multi_query=use_mq,
        mq_variants=mq_variants,
        k_per_variant=k_per_variant,
        mq_prompt_template=mq_prompt_template,
    )


def render_generation_settings(container: DeltaGenerator, key_prefix: str = "sp") -> SearchParams:
    """Отрисовать настройки генерации (LLM) и вернуть SearchParams с параметрами генерации."""
    defaults = SearchParams()
    lim = SearchLimits
    p = key_prefix

    with container.popover("Настройки генерации", use_container_width=True):

        col_t, col_tp = st.columns(2)
        with col_t:
            temperature = st.slider(
                "Температура",
                min_value=lim.temperature.min, max_value=lim.temperature.max,
                value=defaults.temperature, step=lim.temperature.step,
                help="0 = детерминированный, 2 = максимальная креативность",
                key=f"{p}_temperature",
            )
        with col_tp:
            top_p = st.slider(
                "Top-p (nucleus)",
                min_value=lim.top_p.min, max_value=lim.top_p.max,
                value=defaults.top_p, step=lim.top_p.step,
                help="Отсекает маловероятные токены. 1.0 = без ограничений",
                key=f"{p}_top_p",
            )

        use_max_tokens = st.checkbox(
            "Ограничить длину ответа",
            value=defaults.has_max_tokens,
            key=f"{p}_use_max_tokens",
        )
        max_tokens: int | None = None
        if use_max_tokens:
            max_tokens = st.slider(
                "Макс. токенов",
                min_value=lim.max_tokens.min, max_value=lim.max_tokens.max,
                value=defaults.max_tokens_or_default, step=lim.max_tokens.step,
                help="Максимальная длина ответа модели в токенах",
                key=f"{p}_max_tokens",
            )

        col_fp, col_pp = st.columns(2)
        with col_fp:
            frequency_penalty = st.slider(
                "Frequency penalty",
                min_value=lim.frequency_penalty.min, max_value=lim.frequency_penalty.max,
                value=defaults.frequency_penalty, step=lim.frequency_penalty.step,
                help="Штраф за повторение слов. >0 = меньше повторов",
                key=f"{p}_freq_penalty",
            )
        with col_pp:
            presence_penalty = st.slider(
                "Presence penalty",
                min_value=lim.presence_penalty.min, max_value=lim.presence_penalty.max,
                value=defaults.presence_penalty, step=lim.presence_penalty.step,
                help="Штраф за повторение тем. >0 = больше разнообразия",
                key=f"{p}_pres_penalty",
            )

    return SearchParams(
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
    )


# ---------------------------------------------------------------------------
# Настройки промптов
# ---------------------------------------------------------------------------

def render_prompt_settings(container: DeltaGenerator, key_prefix: str = "pp") -> PromptParams:
    """Отрисовать панель настроек промптов в popover и вернуть PromptParams."""
    defaults = PromptParams()
    p = key_prefix

    with container.popover("Промпты", use_container_width=True):
        system_prompt = st.text_area(
            "Системный промпт",
            value=defaults.system_prompt,
            height=PromptLimits.system_prompt_height,
            help="Инструкция для модели — задаёт роль и ограничения",
            key=f"{p}_system",
        )
        user_template = st.text_area(
            "Шаблон пользовательского сообщения",
            value=defaults.user_template,
            height=PromptLimits.user_template_height,
            help="Плейсхолдеры: {context} — текст из базы знаний, {query} — вопрос",
            key=f"{p}_user",
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
    lim = ChunkingLimits

    with st.expander("Настройки чанкинга", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            max_tokens = st.slider(
                "Макс. токенов на чанк",
                min_value=lim.max_tokens.min, max_value=lim.max_tokens.max,
                value=defaults.max_tokens, step=lim.max_tokens.step,
                help="Максимальный размер одного чанка в токенах",
                key=f"{key_prefix}_max_tokens",
            )
        with col2:
            similarity = st.slider(
                "Порог схожести",
                min_value=lim.similarity_threshold.min, max_value=lim.similarity_threshold.max,
                value=defaults.similarity_threshold, step=lim.similarity_threshold.step,
                help="Параграфы со схожестью выше порога объединяются в один чанк",
                key=f"{key_prefix}_similarity",
            )
        with col3:
            embedding_timeout = st.slider(
                "Таймаут embedding (сек)",
                min_value=lim.embedding_timeout.min, max_value=lim.embedding_timeout.max,
                value=defaults.embedding_timeout, step=lim.embedding_timeout.step,
                help="Максимальное время ожидания ответа от сервиса эмбеддингов",
                key=f"{key_prefix}_emb_timeout",
            )

    return ChunkingParams(
        max_tokens=max_tokens,
        similarity_threshold=similarity,
        embedding_timeout=embedding_timeout,
    )


# ---------------------------------------------------------------------------
# Настройки загрузки из Confluence (раздельные для pageIds и spaceKey)
# ---------------------------------------------------------------------------

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
    return StorageParams.from_selectbox(batch_size)


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
            lim = SpaceLoadLimits
            api_page_limit = st.slider(
                "Страниц на запрос API",
                min_value=lim.api_page_limit.min, max_value=lim.api_page_limit.max,
                value=defaults.api_page_limit, step=lim.api_page_limit.step,
                help="Размер страницы при пагинации Confluence REST API",
                key="sp_api_page_limit",
            )
        with col2:
            use_max_pages = st.checkbox(
                "Ограничить количество страниц",
                value=defaults.has_max_pages,
                key="sp_use_max_pages",
            )
            max_pages: int | None = None
            if use_max_pages:
                max_pages = st.number_input(
                    "Макс. страниц",
                    min_value=SpaceLoadLimits.max_pages_min, value=defaults.max_pages_or_default,
                    key="sp_max_pages",
                )

        st.divider()
        storage_params = _render_storage_params("sp")

    space_params = SpaceLoadParams(api_page_limit=api_page_limit, max_pages=max_pages)
    return space_params, storage_params
