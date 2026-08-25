"""Переиспользуемые pydantic-типы boba: конфиг-вход (CSV) и LLM-вход инструментов."""

from __future__ import annotations

import csv
import json
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator

__all__ = [
    "LLMStringList",
    "SecretRevealing",
    "StringList",
    "StringLists",
]


class SecretRevealing(BaseModel):
    """Конфиг инструмента, умеющий дамп с раскрытыми секретами.

    Дамп едет только в tool_stdin песочного вызова; обязан собираться обратно
    в тот же тип — SecretStr оживает из открытой строки. Ключ контекста един
    с REVEAL_SECRETS db/http-конфигов: их сериализаторы читают его же.
    """

    REVEAL_CONTEXT: ClassVar[str] = "reveal_secrets"

    def revealed(self) -> dict[str, object]:
        return self.model_dump(mode="json", context={self.REVEAL_CONTEXT: True})


class StringLists:
    """Списки строк из конфига (CSV) и из ответа LLM (json-список, CSV, одно значение).

    CSV читается модулем csv: значение с запятой берётся в кавычки, а не рвётся.
    """

    JSON_START: ClassVar[str] = "["

    @classmethod
    def of_csv(cls, value: Any) -> Any:
        """CSV-строка -> list[str]; не строка — без изменений."""
        if not isinstance(value, str):
            return value

        return cls._csv(value)

    @classmethod
    def of_llm(cls, value: Any) -> Any:
        """LLM-вход: ["a","b"], '["a","b"]', "a,b", "a" -> list[str]."""
        if not isinstance(value, str):
            return value

        text = value.strip()
        if not text.startswith(cls.JSON_START):
            return cls._csv(text)

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

    @classmethod
    def _csv(cls, text: str) -> list[str]:
        items: list[str] = []
        for row in csv.reader(text.splitlines(), skipinitialspace=True):
            for item in row:
                stripped = item.strip()
                if not stripped:
                    continue

                items.append(stripped)

        return items


StringList = Annotated[list[str], BeforeValidator(StringLists.of_csv)]
"""list[str] с CSV-парсингом строкового входа ("a,b,c" -> ["a","b","c"])."""


LLMStringList = Annotated[list[str], BeforeValidator(StringLists.of_llm)]
"""list[str] с LLM-нормализацией входа: ["a","b"], '["a","b"]', "a,b", "a"."""
