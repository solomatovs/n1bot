"""Ошибки framework-слоя tools (declaration / DI registration).

Это плагин-объявления и DI-регистрация, не runtime-выполнение tool'ов.
Ошибки runtime-вызова tool'а живут в `boba.tools.domain.errors`
(`ToolExecutionError`, `InvalidToolArgumentError`, ...).
"""

from __future__ import annotations

__all__ = [
    "DuplicateProviderError",
    "ToolDeclarationError",
    "ToolsFrameworkError",
]


class ToolsFrameworkError(Exception):
    """База для всех ошибок framework-слоя tools."""


class ToolDeclarationError(ToolsFrameworkError):
    """Некорректная декларация tool'а или provider'а.

    Примеры:
    - параметр без аннотации;
    - `*args`/`**kwargs` в подписи tool'а;
    - return type у `@provides` не указан;
    - `FromConfig`-параметр на типе, не являющемся Pydantic-settings.
    """


class DuplicateProviderError(ToolsFrameworkError):
    """Два provider'а для одного типа в одной зоне registration.

    App-уровень и plugin-уровень — **разные зоны**: плагин может
    переопределить тип, который определил app, без конфликта (плагинский
    component увидит свою версию, остальные — app'а). Но **внутри** одной
    зоны два provider'а одного типа — ошибка.
    """
