"""UI-специфичные типы — зависят от Streamlit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import streamlit as st

from models import AppConfig, ChatMessage


# ---------------------------------------------------------------------------
# Границы UI-слайдеров
# ---------------------------------------------------------------------------

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
    embedding_timeout = IntSliderRange(10, 600, 10)


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

    def push_message(self, msg: ChatMessage) -> None:
        self.chat_history.append(msg)
        self.last_prompt_base = msg.question
        self.variants[msg.question] = 0
