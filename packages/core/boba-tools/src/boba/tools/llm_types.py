"""LLM-tolerant pydantic-типы для tool-аргументов.

LLM иногда передаёт list-параметры как JSON-строку (`'["a","b"]'`) или
как одиночную строку (`'a'`) вместо JSON-массива. Эти типы заворачивают
`BeforeValidator`, который нормализует такой вход перед основной
проверкой типа.

Использовать **только** для LLM-args инструментов. Для env/TOML-полей
конфигов остаётся `boba.settings.StringList` (CSV-only).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = ["LLMStringList"]


def _to_string_list(v: Any) -> Any:
    """Привести типовой LLM-вход к `list[str]`.

    - `list`/`None` (и прочее не-`str`) — без изменений.
    - `str`, начинающаяся (после strip) с `[` — пробуем `json.loads`;
      если результат — `list`, возвращаем `[str(x) for x in parsed]`.
    - иначе строку разбиваем по `,` со strip'ом пустых элементов
      (одиночное `"950276"` → `["950276"]`).
    """
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
"""
`list[str]` с LLM нормализацией строкового входа.

Применение в tool-сигнатуре:

    @tool
    def my_tool(
        page_ids: Annotated[LLMStringList, Field(description="...")],
    ) -> ...: ...

Принимает: `["a","b"]`, `'["a","b"]'`, `"a,b"`, `"a"`.
Для nullable — `LLMStringList | None = None`.
"""
