"""UI-специфичные типы — зависят от Streamlit."""
from __future__ import annotations

import streamlit as st

from domain.config import AppConfig


class CacheTTL:
    """Время жизни кэша Streamlit (секунды)."""
    collections: int = 60
    models: int = 60
    preview: int = 20


class SessionState:
    """Типизированная обёртка над ``st.session_state``."""

    def __init__(self, cfg: AppConfig) -> None:
        if "_app_state_init" not in st.session_state:
            st.session_state["_app_state_init"] = True
            st.session_state.selected_collection = cfg.default_collection
            st.session_state.selected_model_name = cfg.default_model

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
