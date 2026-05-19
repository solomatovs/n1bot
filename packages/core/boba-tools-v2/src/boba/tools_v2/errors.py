"""Доменные ошибки framework'а tools-v2."""

from __future__ import annotations

__all__ = [
    "DuplicateProviderError",
    "ToolDeclarationError",
    "ToolsV2Error",
]


class ToolsV2Error(Exception):
    """База для всех ошибок tools-v2."""


class ToolDeclarationError(ToolsV2Error):
    """Некорректная декларация tool'а или provider'а.

    Примеры:
    - параметр без аннотации;
    - `*args`/`**kwargs` в подписи tool'а;
    - return type у `@provides` не указан;
    - `FromConfig`-параметр на типе, не являющемся Pydantic-settings.
    """


class DuplicateProviderError(ToolsV2Error):
    """Два provider'а для одного типа в одной зоне registration.

    Phase 1 (app) и Phase 2 (plugin) — это **разные зоны**: плагин может
    переопределить тип, который определил app, без конфликта (плагинский
    component увидит свою версию, остальные — app'а). Но **внутри** одной
    зоны два provider'а одного типа — ошибка.
    """
