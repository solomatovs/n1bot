"""N1 Hub RAG — точка входа Streamlit UI."""
from __future__ import annotations

import sys
from pathlib import Path

# src/ должен быть в sys.path для абсолютных импортов domain-кода
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Загрузить Streamlit secrets в os.environ ДО любых импортов domain-кода.
# AppConfig читает os.environ при создании — значения должны быть там к этому моменту.
from ui.secrets import load_streamlit_secrets_to_env  # noqa: E402

load_streamlit_secrets_to_env()

# Все импорты domain-кода — после загрузки secrets
import streamlit as st  # noqa: E402

from infrastructure.bootstrap import bootstrap  # noqa: E402
from domain.config import AppConfig  # noqa: E402
from ui.state import SessionState  # noqa: E402
from ui.tabs import confluence_import, data, doc_chat, file_manager  # noqa: E402

# ========================= Конфигурация страницы
st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

# ========================= Bootstrap
cfg = AppConfig()
services = bootstrap(cfg)
state = SessionState(cfg)

# ========================= Вкладки
tab_import, tab_files, tab_doc_chat, tab_data = st.tabs([
    "Загрузка из Confluence", "Документы", "Чат по документам", "Векторное хранилище",
])

with tab_import:
    confluence_import.render(services, state)

with tab_files:
    file_manager.render(services, state)

with tab_doc_chat:
    doc_chat.render(services, state)

with tab_data:
    data.render(services, state)
