"""`@tool` и `@provides` декораторы — навешивают метаданные на объекты.

Это **дешёвые** декораторы: просто проставляют sentinel-атрибуты, чтобы
framework на этапе registration plugin'а мог отличить tool от обычного
class'а и service factory от обычной функции.

Никаких побочных эффектов на момент декорирования — введение и разбор
сигнатуры (build of pydantic args model, DI plan) случается **позже**,
при регистрации в `ToolBuilder`. Это позволяет писать декорации в
module-scope без heavy startup overhead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, overload

from boba.tools.declarative.errors import ToolDeclarationError
from boba.tools.declarative.scope import Scope

__all__ = [
    "is_provider",
    "is_tool",
    "provider_scope",
    "provides",
    "tool",
    "tool_name",
]


_TOOL_MARKER = "__tool__"
"""Атрибут-маркер: класс/функция помечена как tool."""

_TOOL_NAME_MARKER = "__tool_name__"
"""Атрибут-маркер: wire-имя tool'а. Проставляется `@tool` единожды при
декорировании — либо явное из `@tool(name=...)`, либо производное от
`__name__`. `tool_name()` потом только читает его."""

_PROVIDES_SCOPE_MARKER = "__provides_scope__"
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

    Wire-имя tool (как его увидит LLM) вычисляется здесь единожды и кладётся
    в маркер; дальше его читает `tool_name()`:
    - если задан `@tool(name="...")` — используется он;
    - иначе — `obj.__name__` для функций и `type(obj).__name__` для классов/инстансов

    Описание для LLM — из docstring класса/функции.
    """

    def _decorate(o: T) -> T:
        setattr(o, _TOOL_MARKER, True)
        setattr(o, _TOOL_NAME_MARKER, name if name is not None else _derive_name(o))
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
        setattr(func, _PROVIDES_SCOPE_MARKER, scope)
        return func

    if fn is not None:
        return _decorate(fn)

    return _decorate


def is_tool(obj: object) -> bool:
    """True если объект помечен `@tool`."""
    return getattr(obj, _TOOL_MARKER, False) is True


def is_provider(obj: object) -> bool:
    """True если объект помечен `@provides`."""
    return hasattr(obj, _PROVIDES_SCOPE_MARKER)


def provider_scope(obj: object) -> Scope:
    """`Scope` provider'а; падает `AttributeError`, если объект не provider."""
    return getattr(obj, _PROVIDES_SCOPE_MARKER)


def tool_name(obj: object) -> str:
    """Единственный источник wire-имени tool'а — читает маркер `@tool`.

    Маркер проставляется `@tool` единожды при декорировании, поэтому его
    отсутствие означает, что объект не помечен `@tool` — это ошибка
    декларации, а не повод деривить имя на лету.
    """
    marked = getattr(obj, _TOOL_NAME_MARKER, None)
    if marked is None:
        msg = (
            f"{obj!r} не помечен @tool: нет wire-имени. "
            f"Имя проставляется @tool при декорировании."
        )
        raise ToolDeclarationError(msg)
    return marked


def _derive_name(obj: object) -> str:
    """Производное имя из `__name__` (функция/класс) или имени типа.

    Используется только `@tool` для вычисления имени при декорировании.
    """
    return getattr(obj, "__name__", None) or type(obj).__name__
