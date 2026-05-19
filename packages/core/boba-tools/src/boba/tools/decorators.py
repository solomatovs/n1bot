"""`@tool` и `@provides` декораторы — навешивают метаданные на объекты.

Это **дешёвые** декораторы: просто проставляют sentinel-атрибуты, чтобы
framework на этапе registration plugin'а мог отличить tool от обычного
class'а и service factory от обычной функции.

Никаких побочных эффектов на момент декорирования — введение и разбор
сигнатуры (build of pydantic args model, DI plan) случается **позже**,
в `AgentBuilder.build()`. Это позволяет писать декорации в module-scope
без heavy startup overhead.

`enable_if` — опциональный predicate-callable, который framework
вызывает на этапе сборки контейнера ПОСЛЕ резолва FromConfig-типов, но
ДО регистрации tool'а/provider'а в DI. Если возвращает `False` —
объект пропускается. Параметры предиката должны быть только
FromConfig-маркированными (на момент проверки DI ещё не существует).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, overload

from boba.tools.scope import Scope

__all__ = [
    "ENABLE_IF_MARKER",
    "PROVIDES_SCOPE_MARKER",
    "TOOL_MARKER",
    "enable_if_predicate",
    "has_enable_if",
    "is_provider",
    "is_tool",
    "provider_scope",
    "provides",
    "tool",
]


TOOL_MARKER = "__boba_tools_tool__"
"""Атрибут-маркер: класс/функция помечена как tool."""

PROVIDES_SCOPE_MARKER = "__boba_tools_provides_scope__"
"""Атрибут-маркер: функция помечена как service provider; значение — Scope."""

ENABLE_IF_MARKER = "__boba_tools_enable_if__"
"""Атрибут-маркер: предикат `Callable[..., bool]` для условной регистрации."""


T = TypeVar("T")


@overload
def tool(obj: T, /) -> T: ...


@overload
def tool(
    *, enable_if: Callable[..., bool],
) -> Callable[[T], T]: ...


def tool(
    obj: T | None = None,
    /,
    *,
    enable_if: Callable[..., bool] | None = None,
) -> T | Callable[[T], T]:
    """Пометить class или function как tool.

    Класс должен иметь `__call__(self, ...)`. Функция используется как есть.
    Имя автогенерится из `type(obj).__name__` / `obj.__name__` (snake_case,
    стрипается суффикс `Tool`).

    Описание для LLM — из docstring класса/функции.

    `enable_if` — опциональный predicate. Если задан, framework вызовет
    его на этапе сборки контейнера (после резолва FromConfig); при
    `False` tool не будет зарегистрирован.
    """

    def _decorate(o: T) -> T:
        setattr(o, TOOL_MARKER, True)
        if enable_if is not None:
            setattr(o, ENABLE_IF_MARKER, enable_if)
        return o

    if obj is not None:
        return _decorate(obj)
    return _decorate


@overload
def provides(fn: Callable[..., T], /) -> Callable[..., T]: ...


@overload
def provides(
    *,
    scope: Scope = ...,
    enable_if: Callable[..., bool] | None = ...,
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def provides(
    fn: Callable[..., T] | None = None,
    /,
    *,
    scope: Scope = Scope.APP,
    enable_if: Callable[..., bool] | None = None,
) -> Callable[..., T] | Callable[[Callable[..., T]], Callable[..., T]]:
    """Пометить функцию как service factory для DI-контейнера.

    Формы:
        @provides                       # default Scope.APP
        @provides(scope=Scope.APP)      # explicit
        @provides(scope=Scope.REQUEST)  # request-scoped
        @provides(enable_if=...)        # условная регистрация

    Параметры функции — её DI-зависимости (через `FromConfig`/`FromDI`).
    Return-type annotation — тип, под которым служба будет
    зарегистрирована в Container'е. Если return type не указан — framework
    кинет `ToolDeclarationError` на загрузке.

    `enable_if` — predicate, см. `@tool`. На provider'е полезно, если он
    делает side-effect work на первой резолюции, и оператор хочет
    выключить целиком.
    """

    def _decorate(func: Callable[..., T]) -> Callable[..., T]:
        setattr(func, PROVIDES_SCOPE_MARKER, scope)

        if enable_if is not None:
            setattr(func, ENABLE_IF_MARKER, enable_if)

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


def has_enable_if(obj: object) -> bool:
    """True если на объекте задан `enable_if` predicate."""
    return hasattr(obj, ENABLE_IF_MARKER)


def enable_if_predicate(obj: object) -> Callable[..., bool]:
    """`enable_if` predicate. Падает `AttributeError` если не задан."""
    return getattr(obj, ENABLE_IF_MARKER)
