"""Типизированное управление состоянием сессии Streamlit."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List

import streamlit as st

from config import secret


# ---------------------------------------------------------------------------
# Конфигурация приложения из секретов / переменных окружения
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения.

    Все секреты и переменные окружения читаются здесь.
    Остальные модули получают значения через cfg.*.
    """
    chroma_db_path: str = field(default_factory=lambda: Path(secret("CHROMA_DB_PATH")).as_posix())
    litellm_url: str = field(default_factory=lambda: secret("LITELLM_URL"))
    litellm_api_key: str = field(default_factory=lambda: secret("LITELLM_API_KEY"))
    confluence_url: str = field(default_factory=lambda: secret("CONFLUENCE_URL"))
    confluence_token: str = field(default_factory=lambda: secret("CONFLUENCE_TOKEN"))
    default_collection: str = field(default_factory=lambda: secret("DEFAULT_COLLECTION"))
    default_model: str = field(default_factory=lambda: secret("LLM_MODEL"))
    embedding_model: str = field(default_factory=lambda: secret("EMBEDDING_MODEL"))
    llm_timeout: int = field(default_factory=lambda: int(secret("LLM_TIMEOUT", "120")))
    embedding_timeout: int = field(default_factory=lambda: int(secret("EMBEDDING_TIMEOUT", "120")))
    log_level: str = field(default_factory=lambda: secret("LOG_LEVEL", "INFO"))

    @property
    def litellm_base_url(self) -> str:
        """Базовый URL без /v1 — для эмбеддингов и прямых запросов."""
        return self.litellm_url.rstrip("/").removesuffix("/v1")

    @property
    def openai_url(self) -> str:
        """URL с /v1 — для OpenAI-совместимого API."""
        return f"{self.litellm_base_url}/v1"


# ---------------------------------------------------------------------------
# Сообщение чата — один элемент истории переписки
# ---------------------------------------------------------------------------

class ContentType(Enum):
    """Типы контента в векторном хранилище."""
    TEXT = "text", "Текст"
    CODE = "code", "Код"
    TABLE = "table", "Таблицы"
    PARAGRAPH = "paragraph", "Параграфы"
    LIST = "list", "Списки"

    def __init__(self, key: str, label: str) -> None:
        self._key = key
        self._label = label

    @property
    def key(self) -> str:
        return self._key

    @property
    def label(self) -> str:
        return self._label


@dataclass(frozen=True)
class IntSliderRange:
    """Границы и шаг для целочисленного UI-слайдера."""
    min: int
    max: int
    step: int = 1


@dataclass(frozen=True)
class FloatSliderRange:
    """Границы и шаг для дробного UI-слайдера."""
    min: float
    max: float
    step: float = 0.05


# ---------------------------------------------------------------------------
# Границы UI-слайдеров — единый источник для всех вкладок
# ---------------------------------------------------------------------------

class SearchLimits:
    """Границы слайдеров настроек поиска и генерации."""
    top_n = IntSliderRange(1, 30)
    answers_per_variant = IntSliderRange(1, 10)
    per_page = IntSliderRange(1, 5)
    mq_variants = IntSliderRange(1, 5)
    k_per_variant = IntSliderRange(1, 15)
    temperature = FloatSliderRange(0.0, 2.0, 0.05)
    top_p = FloatSliderRange(0.0, 1.0, 0.05)
    max_tokens = IntSliderRange(64, 4096, 64)
    max_tokens_default: int = 1024
    frequency_penalty = FloatSliderRange(-2.0, 2.0, 0.1)
    presence_penalty = FloatSliderRange(-2.0, 2.0, 0.1)


class ChunkingLimits:
    """Границы слайдеров настроек чанкинга."""
    max_tokens = IntSliderRange(100, 2000, 50)
    similarity_threshold = FloatSliderRange(0.0, 1.0, 0.05)


class PromptLimits:
    """Параметры UI для настроек промптов."""
    system_prompt_height: int = 100
    user_template_height: int = 150


class SpaceLoadLimits:
    """Границы слайдеров настроек загрузки пространства."""
    api_page_limit = IntSliderRange(1, 200, 10)
    max_pages_default: int = 100
    max_pages_min: int = 1


class CacheTTL:
    """Время жизни кэша Streamlit (секунды)."""
    collections: int = 60
    models: int = 60
    preview: int = 20


BATCH_SIZE_OPTIONS = [8, 16, 32, 64, 128]


@dataclass
class SearchParams:
    """Параметры поиска и генерации, управляемые пользователем."""
    # -- поиск --
    top_n: int = 12
    answers_per_variant: int = 3
    per_page: int = 1
    content_types: list[str] | None = None
    # -- multi-query --
    use_multi_query: bool = True
    mq_variants: int = 3
    k_per_variant: int = 6
    mq_prompt_template: str = "Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {query}"
    # -- генерация --
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int | None = None
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0

    @property
    def has_max_tokens(self) -> bool:
        """Включено ли ограничение длины ответа."""
        return self.max_tokens is not None

    @property
    def max_tokens_or_default(self) -> int:
        """Значение max_tokens для UI-слайдера (с fallback на дефолт)."""
        return self.max_tokens if self.max_tokens is not None else SearchLimits.max_tokens_default

    def llm_kwargs(self) -> dict:
        """Параметры генерации для передачи в OpenAI API."""
        kwargs: dict = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs


@dataclass
class ChunkingParams:
    """Параметры чанкинга документов."""
    max_tokens: int = 500
    similarity_threshold: float = 0.7


@dataclass
class SpaceLoadParams:
    """Параметры загрузки пространства Confluence."""
    api_page_limit: int = 50
    max_pages: int | None = None

    @property
    def has_max_pages(self) -> bool:
        """Включено ли ограничение количества страниц."""
        return self.max_pages is not None

    @property
    def max_pages_or_default(self) -> int:
        """Значение max_pages для UI (с fallback на дефолт)."""
        return self.max_pages if self.max_pages is not None else SpaceLoadLimits.max_pages_default


@dataclass
class StorageParams:
    """Параметры сохранения в ChromaDB."""
    batch_size: int = 32

    @staticmethod
    def from_selectbox(value: int | None) -> StorageParams:
        """Создать из значения st.selectbox (может быть None)."""
        defaults = StorageParams()
        return StorageParams(batch_size=value if value is not None else defaults.batch_size)


DEFAULT_SYSTEM_PROMPT = (
    "Ты — эксперт по корпоративной базе знаний. "
    "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете."
)
DEFAULT_USER_TEMPLATE = "Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."


@dataclass
class PromptParams:
    """Шаблоны промптов, управляемые пользователем.

    В ``user_template`` доступны плейсхолдеры:
    - ``{context}`` — текст из векторной базы
    - ``{query}`` — вопрос пользователя
    """
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = DEFAULT_USER_TEMPLATE

    def format_user_message(self, context: str, query: str) -> str:
        """Подставить контекст и вопрос в шаблон."""
        return self.user_template.format(context=context, query=query)


@dataclass
class ChatMessage:
    question: str
    answer: str
    thinking: str = ""
    rag_context: str = ""


# ---------------------------------------------------------------------------
# Фасад состояния сессии — типизированный доступ к st.session_state
# ---------------------------------------------------------------------------

_KEY = "_app_state_init"


class SessionState:
    """Типизированная обёртка над ``st.session_state``."""

    def __init__(self, cfg: AppConfig) -> None:
        if _KEY not in st.session_state:
            st.session_state[_KEY] = True
            st.session_state.selected_collection = cfg.default_collection
            st.session_state.selected_model_name = cfg.default_model
            st.session_state.chat_history = []
            st.session_state.last_prompt_base = ""
            st.session_state.variants = {}

    # -- свойства ------------------------------------------------------------

    @property
    def selected_collection(self) -> str:
        return st.session_state.selected_collection

    @selected_collection.setter
    def selected_collection(self, value: str) -> None:
        st.session_state.selected_collection = value

    @property
    def selected_model_name(self) -> str:
        return st.session_state.selected_model_name

    @selected_model_name.setter
    def selected_model_name(self, value: str) -> None:
        st.session_state.selected_model_name = value

    @property
    def chat_history(self) -> List[ChatMessage]:
        return st.session_state.chat_history

    @property
    def last_prompt_base(self) -> str:
        return st.session_state.last_prompt_base

    @last_prompt_base.setter
    def last_prompt_base(self, value: str) -> None:
        st.session_state.last_prompt_base = value

    @property
    def variants(self) -> Dict[str, int]:
        return st.session_state.variants

    # -- вспомогательные методы -----------------------------------------------

    def push_message(self, msg: ChatMessage) -> None:
        self.chat_history.append(msg)
        self.last_prompt_base = msg.question
        self.variants[msg.question] = 0
