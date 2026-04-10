"""UI-специфичные типы — зависят от Streamlit."""
from __future__ import annotations

from domain.config import AppConfig


class CacheTTL:
    """Время жизни кэша Streamlit (секунды)."""
    collections: int = 60
    models: int = 60
    preview: int = 20


class SessionState:
    """Типизированная обёртка над ``st.session_state``."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
