"""Ошибки декларативного слоя tools (declaration / DI registration).

Это плагин-объявления и DI-регистрация, не runtime-выполнение tool'ов.
Ошибки runtime-вызова tool'а живут в `boba.tools.domain.errors`
(`ToolExecutionError`, `InvalidToolArgumentError`, ...), а коллизии
имён/источников при сборке — в `boba.tools.framework.errors`.
"""

from __future__ import annotations

__all__ = [
    "DuplicateProviderError",
    "ToolDeclarationError",
    "ToolsFrameworkError",
    "UnresolvedDependencyError",
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


class UnresolvedDependencyError(ToolsFrameworkError):
    """На сборке обнаружен `FromDI`-маркер без зарегистрированного provider'а.

    Строгая build-time проверка `FromDI`: недостающая служба валит `build()`,
    а не всплывает в runtime при инвоке tool'а.

    `FromConfig` сюда **не** относится — конфиг это обычный provider
    (см. `ToolBuilder.register_config`), и его отсутствие — runtime
    `NoFactoryError`, как у любой незарегистрированной DI-службы.
    """


class DuplicateProviderError(ToolsFrameworkError):
    """Два provider'а для одного типа в одной зоне registration.

    App-уровень и plugin-уровень — **разные зоны**: плагин может
    переопределить тип, который определил app, без конфликта (плагинский
    component увидит свою версию, остальные — app'а). Но **внутри** одной
    зоны два provider'а одного типа — ошибка.
    """
