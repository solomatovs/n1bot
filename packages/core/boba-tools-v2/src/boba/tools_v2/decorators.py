"""`@tool` и `@provides` декораторы — навешивают метаданные на объекты.

Это **дешёвые** декораторы: просто проставляют sentinel-атрибуты, чтобы
framework на этапе registration plugin'а мог отличить tool от обычного
class'а и service factory от обычной функции.

Никаких побочных эффектов на момент декорирования — введение и разбор
сигнатуры (build of pydantic args model, DI plan) случается **позже**,
в `AgentBuilder.build()`. Это позволяет писать декорации в module-scope
без heavy startup overhead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, overload

from boba.tools_v2.scope import Scope

__all__ = [
    "PROVIDES_SCOPE_MARKER",
    "TOOL_MARKER",
    "is_provider",
    "is_tool",
    "provider_scope",
    "provides",
    "tool",
]


TOOL_MARKER = "__boba_tools_v2_tool__"
"""Атрибут-маркер: класс/функция помечена как tool."""

PROVIDES_SCOPE_MARKER = "__boba_tools_v2_provides_scope__"
"""Атрибут-маркер: функция помечена как service provider; значение — Scope."""


T = TypeVar("T")


def tool(obj: T) -> T:
    """Пометить class или function как tool.

    Класс должен иметь `__call__(self, ...)`. Функция используется как есть.
    Имя автогенерится из `type(obj).__name__` / `obj.__name__` (snake_case,
    стрипается суффикс `Tool`).

    Описание для LLM — из docstring класса/функции.
    """
    setattr(obj, TOOL_MARKER, True)
    return obj


@overload
def provides(fn: Callable[..., T], /) -> Callable[..., T]: ...


@overload
def provides(*, scope: Scope) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def provides(
    fn: Callable[..., T] | None = None,
    /,
    *,
    scope: Scope = Scope.APP,
) -> Callable[..., T] | Callable[[Callable[..., T]], Callable[..., T]]:
    """Пометить функцию как service factory для DI-контейнера.

    Формы:
        @provides                       # default Scope.APP
        @provides(scope=Scope.APP)      # explicit
        @provides(scope=Scope.REQUEST)  # request-scoped

    Параметры функции — её DI-зависимости (через `FromConfig`/`FromDI`).
    Return-type annotation — тип, под которым служба будет
    зарегистрирована в Container'е. Если return type не указан — framework
    кинет `ToolDeclarationError` на загрузке.
    """

    def _decorate(func: Callable[..., T]) -> Callable[..., T]:
        setattr(func, PROVIDES_SCOPE_MARKER, scope)
        return func

    if fn is not None:
        return _decorate(fn)
    return _decorate


def is_tool(obj: object) -> bool:
    """True если объект помечен `@tool`."""
    return getattr(obj, TOOL_MARKER, False) is True


def is_provider(obj: object) -> bool:
    """True если функция помечена `@provides`."""
    return hasattr(obj, PROVIDES_SCOPE_MARKER)


def provider_scope(obj: object) -> Scope:
    """`Scope` provider'а. Падает `AttributeError` если объект не provider."""
    return getattr(obj, PROVIDES_SCOPE_MARKER)
