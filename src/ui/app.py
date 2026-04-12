"""N1 Hub RAG — точка входа Streamlit UI."""
from __future__ import annotations

import sys
from pathlib import Path

# src/ должен быть в sys.path для абсолютных импортов domain-кода
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Загрузить Streamlit secrets в os.environ ДО любых импортов domain-кода.
from ui.secrets import load_streamlit_secrets_to_env  # noqa: E402

load_streamlit_secrets_to_env()

# Все импорты domain-кода — после загрузки secrets
import streamlit as st  # noqa: E402

from infrastructure.container import create_container  # noqa: E402
from domain.config import AppConfig  # noqa: E402
from ui.state import SessionState  # noqa: E402
from ui.tabs import confluence_import, data, doc_chat, file_manager  # noqa: E402

# ========================= Конфигурация страницы
st.set_page_config(page_title="N1 Hub RAG — MQ", layout="wide")

# ========================= DI Container
container = create_container()
cfg = container.get(AppConfig)
state = SessionState(cfg)

# ========================= Вкладки
tab_import, tab_upload, tab_vector, tab_chat = st.tabs([
    "Загрузка из Confluence",
    "Загрузка вручную",
    "Векторное хранилище",
    "Чат по документам",
])

with tab_import:
    confluence_import.render(container, state)

with tab_upload:
    file_manager.render(container, state)

with tab_vector:
    data.render(container, state)

with tab_chat:
    doc_chat.render(container, state)
