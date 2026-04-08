"""Утилиты конфигурации — чистый Python, без Streamlit."""
from __future__ import annotations

import os
from typing import List


def secret(key: str, default: str = "") -> str:
    """Получить значение из переменной окружения."""
    return os.environ.get(key, default)


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
