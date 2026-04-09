"""Формы настроек — retrieval, multi-query, generation, prompts, chunking, загрузка."""
from __future__ import annotations

from dataclasses import dataclass

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from domain.chat import PromptParams
from domain.loading import (
    BATCH_SIZE_OPTIONS,
    ChunkingParams,
    ConfluenceContentFormat,
    ConfluenceLoaderParams,
    SpaceLoadParams,
    StorageParams,
)
from domain.search import SearchParams
from ui.components.utils import safe_index
from ui.state import (
    ChunkingLimits,
    ConfluenceLimits,
    FloatSliderRange,
    IntSliderRange,
    PromptLimits,
    SearchLimits,
    SpaceLoadLimits,
)




# ---------------------------------------------------------------------------
# Настройки поиска
# ---------------------------------------------------------------------------

def render_retrieval_settings(container: DeltaGenerator, key_prefix: str = "sp") -> SearchParams:
    """Настройки поиска (глубина, per_page, типы контента)."""
    defaults = SearchParams()
    lim = SearchLimits
    p = key_prefix

    with container.popover("Настройки поиска", use_container_width=True):
        col1, col2 = st.columns(2)
        with col1:
            top_n = _int_slider("Глубина поиска", lim.top_n, defaults.top_n,
                                help="Сколько кандидатов извлекать из векторной базы", key=f"{p}_top_n")
            per_page = _int_slider("Чанков с одной страницы", lim.per_page, defaults.per_page,
                                   help="Максимум чанков с одной Confluence-страницы (дедупликация)", key=f"{p}_per_page")
        with col2:
            answers = _int_slider("Документов в контекст", lim.answers_per_variant, defaults.answers_per_variant,
                                  help="Сколько финальных чанков отдать модели для генерации ответа", key=f"{p}_answers")

        content_types = _content_type_multiselect(key=f"{p}_content_types")

    return SearchParams(
        top_n=top_n,
        answers_per_variant=answers,
        per_page=per_page,
        content_types=content_types,
    )


# ---------------------------------------------------------------------------
# Multi-query
# ---------------------------------------------------------------------------

def render_multiquery_settings(container: DeltaGenerator, key_prefix: str = "mq") -> SearchParams:
    """Настройки multi-query (переформулировки + RRF)."""
    defaults = SearchParams()
    lim = SearchLimits
    p = key_prefix

    with container.popover("Multi-query", use_container_width=True):
        use_mq = st.checkbox(
            "Включить переформулировки + RRF",
            value=defaults.use_multi_query,
            key=f"{p}_use_mq",
        )
        col1, col2 = st.columns(2)
        with col1:
            mq_variants = _int_slider("Переформулировок", lim.mq_variants, defaults.mq_variants,
                                      disabled=not use_mq,
                                      help="Количество вариантов запроса для multi-query", key=f"{p}_mq_variants")
        with col2:
            k_per_variant = _int_slider("Документов на вариант", lim.k_per_variant, defaults.k_per_variant,
                                        disabled=not use_mq,
                                        help="Сколько документов извлекать на каждую переформулировку", key=f"{p}_k_per_var")
        mq_prompt_template = st.text_area(
            "Промпт переформулировки",
            value=defaults.mq_prompt_template,
            disabled=not use_mq,
            help="Плейсхолдеры: {n} — количество, {query} — запрос пользователя",
            key=f"{p}_mq_prompt",
        )

    return SearchParams(
        use_multi_query=use_mq,
        mq_variants=mq_variants,
        k_per_variant=k_per_variant,
        mq_prompt_template=mq_prompt_template,
    )


# ---------------------------------------------------------------------------
# Настройки генерации
# ---------------------------------------------------------------------------

def render_generation_settings(container: DeltaGenerator, key_prefix: str = "sp") -> SearchParams:
    """Настройки генерации (temperature, top_p, penalties, max_tokens)."""
    defaults = SearchParams()
    lim = SearchLimits
    p = key_prefix

    with container.popover("Настройки генерации", use_container_width=True):
        col_t, col_tp = st.columns(2)
        with col_t:
            temperature = _float_slider("Температура", lim.temperature, defaults.temperature,
                                        help="0 = детерминированный, 2 = максимальная креативность", key=f"{p}_temperature")
        with col_tp:
            top_p = _float_slider("Top-p (nucleus)", lim.top_p, defaults.top_p,
                                  help="Отсекает маловероятные токены. 1.0 = без ограничений", key=f"{p}_top_p")

        max_tokens = _optional_int_slider(
            label="Макс. токенов",
            checkbox_label="Ограничить длину ответа",
            limit=lim.max_tokens,
            default=defaults.max_tokens,
            fallback=lim.max_tokens_default,
            help="Максимальная длина ответа модели в токенах",
            key_prefix=f"{p}_max_tokens",
        )

        col_fp, col_pp = st.columns(2)
        with col_fp:
            frequency_penalty = _float_slider("Frequency penalty", lim.frequency_penalty, defaults.frequency_penalty,
                                               help="Штраф за повторение слов. >0 = меньше повторов", key=f"{p}_freq_penalty")
        with col_pp:
            presence_penalty = _float_slider("Presence penalty", lim.presence_penalty, defaults.presence_penalty,
                                             help="Штраф за повторение тем. >0 = больше разнообразия", key=f"{p}_pres_penalty")

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
    """Настройки системного промпта и шаблона пользовательского сообщения."""
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

    return PromptParams(system_prompt=system_prompt, user_template=user_template)


# ---------------------------------------------------------------------------
# Настройки чанкинга
# ---------------------------------------------------------------------------

def render_chunking_settings(key_prefix: str = "cp") -> ChunkingParams:
    """Настройки чанкинга документов."""
    defaults = ChunkingParams()
    lim = ChunkingLimits
    p = key_prefix

    with st.expander("Настройки чанкинга", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            max_tokens = _int_slider("Макс. токенов на чанк", lim.max_tokens, defaults.max_tokens,
                                     help="Максимальный размер одного чанка в токенах", key=f"{p}_max_tokens")
        with col2:
            similarity = _float_slider("Порог схожести", lim.similarity_threshold, defaults.similarity_threshold,
                                       help="Параграфы со схожестью выше порога объединяются в один чанк", key=f"{p}_similarity")
        with col3:
            embedding_timeout = _int_slider("Таймаут embedding (сек)", lim.embedding_timeout, defaults.embedding_timeout,
                                            help="Максимальное время ожидания ответа от сервиса эмбеддингов", key=f"{p}_emb_timeout")

    return ChunkingParams(
        max_tokens=max_tokens,
        similarity_threshold=similarity,
        embedding_timeout=embedding_timeout,
    )


# ---------------------------------------------------------------------------
# Настройки загрузки
# ---------------------------------------------------------------------------

def render_confluence_loader_settings(key_prefix: str = "cf") -> ConfluenceLoaderParams:
    """Настройки загрузчика Confluence — формат, вложения, retry, транспорт."""
    defaults = ConfluenceLoaderParams()
    lim = ConfluenceLimits
    p = key_prefix

    with st.expander("Настройки Confluence", expanded=False):
        st.markdown("##### Формат контента")
        format_labels = [f.label for f in ConfluenceContentFormat]
        default_idx = safe_index(format_labels, defaults.content_format.label)
        selected_label = st.selectbox("Формат страницы", format_labels, index=default_idx, key=f"{p}_format")
        content_format = _label_to_content_format(selected_label or defaults.content_format.label)

        col1, col2 = st.columns(2)
        with col1:
            keep_markdown = st.checkbox("Сохранять Markdown", value=defaults.keep_markdown_format, key=f"{p}_md")
        with col2:
            keep_newlines = st.checkbox("Сохранять переносы строк", value=defaults.keep_newlines, key=f"{p}_nl")

        st.markdown("##### Дополнительный контент")
        col3, col4, col5 = st.columns(3)
        with col3:
            attachments = st.checkbox("Вложения", value=defaults.include_attachments, key=f"{p}_attach")
        with col4:
            comments = st.checkbox("Комментарии", value=defaults.include_comments, key=f"{p}_comments")
        with col5:
            labels = st.checkbox("Метки", value=defaults.include_labels, key=f"{p}_labels")

        st.markdown("##### Retry и транспорт")
        col6, col7, col8 = st.columns(3)
        with col6:
            retries = _int_slider("Количество retry", lim.number_of_retries, defaults.number_of_retries, key=f"{p}_retries")
        with col7:
            min_retry = _int_slider("Мин. пауза retry (сек)", lim.min_retry_seconds, defaults.min_retry_seconds, key=f"{p}_min_retry")
        with col8:
            max_retry = _int_slider("Макс. пауза retry (сек)", lim.max_retry_seconds, defaults.max_retry_seconds, key=f"{p}_max_retry")

        col9, col10 = st.columns(2)
        with col9:
            timeout = _int_slider("Таймаут (сек)", lim.timeout, defaults.timeout, key=f"{p}_timeout")
        with col10:
            ssl_verify = st.checkbox("Проверять SSL", value=defaults.ssl_verify, key=f"{p}_ssl")

    return ConfluenceLoaderParams(
        content_format=content_format,
        keep_markdown_format=keep_markdown,
        keep_newlines=keep_newlines,
        include_attachments=attachments,
        include_comments=comments,
        include_labels=labels,
        number_of_retries=retries,
        min_retry_seconds=min_retry,
        max_retry_seconds=max_retry,
        timeout=timeout,
        ssl_verify=ssl_verify,
    )


@dataclass(frozen=True)
class SpaceSettings:
    """Результат UI-формы настроек загрузки пространства."""
    space_params: SpaceLoadParams
    storage_params: StorageParams


def render_page_id_settings() -> StorageParams:
    """Настройки для режима загрузки по Page IDs."""
    with st.expander("Настройки загрузки", expanded=False):
        return _render_storage_params("pid")


def render_space_settings() -> SpaceSettings:
    """Настройки для режима загрузки пространства."""
    defaults = SpaceLoadParams()
    lim = SpaceLoadLimits

    with st.expander("Настройки загрузки", expanded=False):
        st.markdown("##### Пространство")
        col1, col2 = st.columns(2)
        with col1:
            api_page_limit = _int_slider(
                "Страниц на запрос API", lim.api_page_limit, defaults.api_page_limit,
                help="Размер страницы при пагинации Confluence REST API", key="sp_api_page_limit",
            )
        with col2:
            max_pages = _optional_int_input(
                checkbox_label="Ограничить количество страниц",
                input_label="Макс. страниц",
                default=defaults.max_pages,
                fallback=lim.max_pages_default,
                min_value=lim.max_pages_min,
                key_prefix="sp",
            )

        st.divider()
        storage_params = _render_storage_params("sp")

    space_params = SpaceLoadParams(api_page_limit=api_page_limit, max_pages=max_pages)
    return SpaceSettings(space_params=space_params, storage_params=storage_params)


def _render_storage_params(key_prefix: str) -> StorageParams:
    """Параметры сохранения в векторное хранилище."""
    defaults = StorageParams()
    batch_size = st.selectbox(
        "Размер батча",
        options=BATCH_SIZE_OPTIONS,
        index=safe_index(BATCH_SIZE_OPTIONS, defaults.batch_size),
        help="Количество документов, сохраняемых за одну операцию",
        key=f"{key_prefix}_batch_size",
    )
    return StorageParams(batch_size=batch_size if batch_size is not None else defaults.batch_size)


# ---------------------------------------------------------------------------
# Приватные хелперы для повторяющихся паттернов Streamlit-виджетов
# ---------------------------------------------------------------------------

def _int_slider(
    label: str,
    limit: IntSliderRange,
    value: int,
    *,
    help: str = "",
    key: str = "",
    disabled: bool = False,
) -> int:
    """Целочисленный слайдер."""
    return st.slider(
        label, min_value=limit.min, max_value=limit.max,
        value=value, step=limit.step, help=help, key=key, disabled=disabled,
    )


def _float_slider(
    label: str,
    limit: FloatSliderRange,
    value: float,
    *,
    help: str = "",
    key: str = "",
    disabled: bool = False,
) -> float:
    """Дробный слайдер."""
    return st.slider(
        label, min_value=limit.min, max_value=limit.max,
        value=value, step=limit.step, help=help, key=key, disabled=disabled,
    )


def _optional_int_slider(
    label: str,
    checkbox_label: str,
    limit: IntSliderRange,
    default: int | None,
    fallback: int,
    help: str,
    key_prefix: str,
) -> int | None:
    """Чекбокс + целочисленный слайдер. Если чекбокс выключен — None."""
    use = st.checkbox(checkbox_label, value=default is not None, key=f"{key_prefix}_use")
    if not use:
        return None
    return _int_slider(label, limit, default if default is not None else fallback, help=help, key=key_prefix)


def _optional_int_input(
    checkbox_label: str,
    input_label: str,
    default: int | None,
    fallback: int,
    min_value: int,
    key_prefix: str,
) -> int | None:
    """Чекбокс + number_input. Если чекбокс выключен — None."""
    use = st.checkbox(checkbox_label, value=default is not None, key=f"{key_prefix}_use_max_pages")
    if not use:
        return None
    return st.number_input(
        input_label,
        min_value=min_value,
        value=default if default is not None else fallback,
        key=f"{key_prefix}_max_pages",
    )


def _content_type_multiselect(key: str) -> list[str] | None:
    """Мультиселект типов контента. Пустой выбор → None (автоопределение)."""
    from domain.loading import ContentType

    chosen_labels = st.multiselect(
        "Типы контента",
        options=[ct.label for ct in ContentType],
        default=[],
        help="Пусто = автоопределение по запросу. Иначе поиск только по выбранным типам",
        key=key,
    )
    return ContentType.labels_to_keys(chosen_labels)


def _label_to_content_format(label: str) -> ConfluenceContentFormat:
    """Преобразовать label в ConfluenceContentFormat."""
    for fmt in ConfluenceContentFormat:
        if fmt.label == label:
            return fmt
    return ConfluenceContentFormat.EXPORT_VIEW
