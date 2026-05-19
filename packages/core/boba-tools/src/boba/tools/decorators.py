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

from boba.tools.scope import Scope

__all__ = [
    "PROVIDES_SCOPE_MARKER",
    "TOOL_MARKER",
    "TOOL_NAME_MARKER",
    "is_provider",
    "is_tool",
    "provider_scope",
    "provides",
    "tool",
    "tool_explicit_name",
]


TOOL_MARKER = "__boba_tools_tool__"
"""Атрибут-маркер: класс/функция помечена как tool."""

TOOL_NAME_MARKER = "__boba_tools_tool_name__"
"""Атрибут-маркер: явное wire-имя tool'а, заданное через `@tool(name=...)`."""

PROVIDES_SCOPE_MARKER = "__boba_tools_provides_scope__"
"""Атрибут-маркер: функция помечена как service provider; значение — Scope."""


T = TypeVar("T")


@overload
def tool(obj: T, /) -> T: ...


@overload
def tool(
    *,
    name: str | None = ...,
) -> Callable[[T], T]: ...


def tool(
    obj: T | None = None,
    /,
    *,
    name: str | None = None,
) -> T | Callable[[T], T]:
    """
    Пометить class или function как tool

    Класс должен иметь `__call__(self, ...)`
    Функция используется как есть

    Wire-имя tool (как его увидит LLM):
    - если задан `@tool(name="...")` — используется он;
    - иначе — `obj.__name__` для функций и `type(obj).__name__` для классов/инстансов

    Описание для LLM — из docstring класса/функции.
    """

    def _decorate(o: T) -> T:
        setattr(o, TOOL_MARKER, True)

        if name is not None:
            setattr(o, TOOL_NAME_MARKER, name)

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
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def provides(
    fn: Callable[..., T] | None = None,
    /,
    *,
    scope: Scope = Scope.APP,
) -> Callable[..., T] | Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Делаем функцию фабрикой объекта для DI

    Формы:
        @provides                       # Scope.APP
        @provides(scope=Scope.APP)      # живет на протяжении времени Agent
        @provides(scope=Scope.REQUEST)  # живет в процессе request
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


def tool_explicit_name(obj: object) -> str | None:
    """Явное wire-имя из `@tool(name=...)` или `None`, если не задано."""
    return getattr(obj, TOOL_NAME_MARKER, None)
