"""Загрузка Streamlit secrets в переменные окружения."""
from __future__ import annotations

import os

import streamlit as st


def load_streamlit_secrets_to_env() -> None:
    """Загрузить st.secrets в os.environ (без перезаписи существующих)."""
    try:
        for key, value in st.secrets.items():
            if isinstance(value, str):
                os.environ.setdefault(key, value)
    except Exception:
        pass
