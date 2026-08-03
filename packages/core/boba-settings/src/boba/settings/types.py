"""Переиспользуемые pydantic-типы для boba-конфигов."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = ["LLMStringList", "StringList"]


def _csv_to_list(v: Any) -> Any:
    """CSV-строка -> list[str]; list/None — без изменений."""
    if isinstance(v, str):
        return [item.strip() for item in v.split(",") if item.strip()]
    return v


StringList = Annotated[list[str], BeforeValidator(_csv_to_list)]
"""list[str] с CSV-парсингом строкового входа ("a,b,c" -> ["a","b","c"])."""


def _to_string_list(v: Any) -> Any:
    """Привести LLM-вход (list, JSON-строка, CSV, одиночное значение) к list[str]."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.startswith("["):
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return v
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return v
    return [item.strip() for item in s.split(",") if item.strip()]


LLMStringList = Annotated[list[str], BeforeValidator(_to_string_list)]
"""list[str] с LLM-нормализацией входа: ["a","b"], '["a","b"]', "a,b", "a"."""
