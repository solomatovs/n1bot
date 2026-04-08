"""N1 Hub RAG — точка входа Streamlit UI."""
from __future__ import annotations

import sys

# sqlite hack для некоторых окружений
try:
    import pysqlite3  # type: ignore  # noqa: F401

    sys.modules["sqlite3"] = sys.modules["pysqlite3"]
except Exception:
    pass

import os

import streamlit as st

# Загрузить Streamlit secrets в env (для config.secret() без зависимости от st)
try:
    for key, value in st.secrets.items():
        if isinstance(value, str):
            os.environ.setdefault(key, value)
except Exception:
    pass

from bootstrap import bootstrap
from tabs import chat, data, load, search
from ui.state import AppConfig, SessionState

# ========================= Конфигурация страницы
st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

# ========================= Bootstrap
cfg = AppConfig()
services = bootstrap(cfg)
state = SessionState(cfg)

# ========================= Вкладки
tab_chat, tab_search, tab_load, tab_data = st.tabs([
    "Чат", "Поиск", "Загрузка из Confluence", "Векторное хранилище",
])

with tab_chat:
    chat.render(services, state)

with tab_search:
    search.render(services, state)

with tab_load:
    load.render(services, state)

with tab_data:
    data.render(services, state)
