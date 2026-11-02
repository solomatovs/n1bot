"""Общие Pydantic-типы списков: CSV-конфиг и вход инструментов.

Ошибки:
csv.Error — некорректные CSV-данные при разборе строкового входа.
"""

from __future__ import annotations

import csv
import json
from itertools import chain
from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = [
    "LLMStringList",
    "StringList",
    "StringLists",
]


class StringLists:
    """Валидаторы StringList и LLMStringList; CSV разбирает стандартный csv."""

    def of_csv(self, value: Any) -> Any:
        """CSV-строка -> list[str]; не строка — без изменений."""
        if not isinstance(value, str):
            return value

        return self._csv(value)

    def of_llm(self, value: Any) -> Any:
        """LLM-вход: ["a","b"], '["a","b"]', "a,b", "a" -> list[str]."""
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text.startswith("["):
            return self._csv(text)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value

        if not isinstance(parsed, list):
            return value

        items: list[str] = []
        for item in parsed:
            items.append(str(item))

        return items

    def _csv(self, text: str) -> list[str]:
        rows = csv.reader(text.splitlines(), skipinitialspace=True)
        values = chain.from_iterable(rows)

        items: list[str] = []
        for item in values:
            stripped = item.strip()
            if not stripped:
                continue

            items.append(stripped)

        return items


StringList = Annotated[list[str], BeforeValidator(StringLists().of_csv)]
"""list[str] с CSV-парсингом строкового входа ("a,b,c" -> ["a","b","c"])."""


LLMStringList = Annotated[list[str], BeforeValidator(StringLists().of_llm)]
"""list[str] с LLM-нормализацией входа: ["a","b"], '["a","b"]', "a,b", "a"."""
