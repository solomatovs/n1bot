"""N1 Hub RAG — Streamlit UI entry point."""
from __future__ import annotations

import sys

# sqlite hack для некоторых окружений
try:
    import pysqlite3  # type: ignore

    sys.modules["sqlite3"] = sys.modules["pysqlite3"]
except Exception:
    pass

import streamlit as st

from tabs import chat, data, load
from ui.state import AppConfig, SessionState

# ========================= Page config
st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

# ========================= Shared state
cfg = AppConfig()
state = SessionState(cfg)

# ========================= Tabs
tab_chat, tab_load, tab_data = st.tabs(["Чат", "Загрузка из Confluence", "Векторное хранилище"])

with tab_chat:
    chat.render(cfg, state)

with tab_load:
    load.render(cfg, state)

with tab_data:
    data.render(cfg, state)
