"""Типизированное управление состоянием сессии Streamlit."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Set

import streamlit as st

from config import secret


# ---------------------------------------------------------------------------
# Конфигурация приложения из секретов / переменных окружения
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    chroma_db_path: str = field(default_factory=lambda: Path(secret("CHROMA_DB_PATH")).as_posix())
    litellm_url: str = field(default_factory=lambda: secret("LITELLM_URL"))
    litellm_api_key: str = field(default_factory=lambda: secret("LITELLM_API_KEY"))
    confluence_url: str = field(default_factory=lambda: secret("CONFLUENCE_URL"))
    confluence_token: str = field(default_factory=lambda: secret("CONFLUENCE_TOKEN"))
    default_collection: str = field(default_factory=lambda: secret("DEFAULT_COLLECTION"))
    default_model: str = field(default_factory=lambda: secret("LLM_MODEL"))

    @property
    def openai_url(self) -> str:
        return f"{self.litellm_url.rstrip('/')}/v1"


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
    # -- генерация --
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int | None = None
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0

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
            st.session_state.used_page_ids = {}

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

    @property
    def used_page_ids(self) -> Dict[str, Set[str]]:
        return st.session_state.used_page_ids

    # -- вспомогательные методы -----------------------------------------------

    def push_message(self, msg: ChatMessage) -> None:
        self.chat_history.append(msg)
        self.last_prompt_base = msg.question
        self.variants[msg.question] = 0
