from __future__ import annotations

import os

import ssl
import warnings
import urllib3
from typing import List

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
    """Return env var if set, otherwise fall back to st.secrets."""
    val = os.environ.get(key)
    if val is not None:
        return val
    try:
        val = st.secrets.get(key, default)
        return val
    except Exception:
        return default


EMBEDDING_MODEL: str = secret("EMBEDDING_MODEL")
LLM_TIMEOUT: int = int(secret("LLM_TIMEOUT", "120"))
EMBEDDING_TIMEOUT: int = int(secret("EMBEDDING_TIMEOUT", "120"))

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
