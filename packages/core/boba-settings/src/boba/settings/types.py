"""Переиспользуемые pydantic-типы для boba-конфигов."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = ["LLMStringList", "StringList"]


def _csv_to_list(v: Any) -> Any:
    """CSV-строка → list[str]; list/None — без изменений.

    Используется как `BeforeValidator` для полей типа `list[str]`, чтобы
    значение из env-переменной или TOML-строки `"a,b,c"` нормально
    разворачивалось в `["a", "b", "c"]`. Список и `None` пропускаются
    как есть — pydantic дальше провалидирует тип.
    """
    if isinstance(v, str):
        return [item.strip() for item in v.split(",") if item.strip()]
    return v


StringList = Annotated[list[str], BeforeValidator(_csv_to_list)]
"""`list[str]` с CSV-парсингом строкового входа.

Применение:

    class MyConfig(BaseModel):
        tags: StringList = []                  # "a,b,c" → ["a","b","c"]
        accept: StringList | None = None       # nullable-вариант
"""


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
