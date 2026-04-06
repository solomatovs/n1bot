"""Утилиты конфигурации — secret() и tiktoken encoder."""
from __future__ import annotations

import os
import ssl
import warnings
from typing import List

import urllib3

import streamlit as st

# SSL: отключаем проверку и предупреждения глобально
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context


# ---------------------------------------------------------------------------
# Streamlit secrets (env vars override)
# ---------------------------------------------------------------------------


def secret(key: str, default: str = "") -> str:
    """Получить значение из env var, иначе из st.secrets."""
    val = os.environ.get(key)
    if val is not None:
        return val
    try:
        val = st.secrets.get(key, default)
        return val
    except Exception:
        return default


# ---------------------------------------------------------------------------
# tiktoken (офлайн-безопасно)
# ---------------------------------------------------------------------------
try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None


def _approx_token_count_bytes(b: bytes) -> int:
    return (len(b) + 3) // 4


class _ApproxEncoder:
    def encode(self, s: str) -> List[int]:
        return [0] * _approx_token_count_bytes(s.encode("utf-8"))


def get_tiktoken_encoder():
    if tiktoken is None:
        return _ApproxEncoder()
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return _ApproxEncoder()


enc = get_tiktoken_encoder()
