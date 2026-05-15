"""Переиспользуемые pydantic-типы для boba-конфигов."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator

__all__ = ["StringList"]


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

    class MyConfig(BobaFlatSettings):
        tags: StringList = []                  # env "a,b,c" → ["a","b","c"]
        accept: StringList | None = None       # nullable-вариант
"""
