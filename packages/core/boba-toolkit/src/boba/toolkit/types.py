"""Переиспользуемые pydantic-типы boba: конфиг-вход (CSV) и LLM-вход инструментов."""

from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator

__all__ = ["LLMStringList", "SecretRevealing", "StringList"]


class SecretRevealing(BaseModel):
    """Конфиг инструмента, умеющий дамп с раскрытыми секретами.

    Дамп едет только в tool_stdin песочного вызова; обязан собираться обратно
    в тот же тип — SecretStr оживает из открытой строки. Ключ контекста един
    с REVEAL_SECRETS db/http-конфигов: их сериализаторы читают его же.
    """

    REVEAL_CONTEXT: ClassVar[str] = "reveal_secrets"

    def revealed(self) -> dict[str, object]:
        return self.model_dump(mode="json", context={self.REVEAL_CONTEXT: True})


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
