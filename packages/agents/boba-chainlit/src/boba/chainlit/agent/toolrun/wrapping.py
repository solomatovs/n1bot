"""Постановка обёрток на тело langchain-инструмента.

Обвязки (логи, права, отмена, тап потока, ошибки) отличаются только крючками
вокруг вызова, а способ постановки один: у инструмента два тела — sync-функция
и корутина, — и обёртка должна лечь на оба. Sync/async-близнецы живут здесь
один раз; обвязка описывает только свои крючки CallHooks — они синхронные и
работают в обоих телах. Инструменты приложения строит декоратор `@tool`, то
есть это StructuredTool; тула другого класса обвязка не касается.

Ошибки: своих не выпускает; исключения крючков идут наверх как есть.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import Any, TypeAlias

from langchain_core.tools import BaseTool, StructuredTool

__all__ = ["AsyncCall", "CallHooks", "SyncCall", "ToolBody"]

SyncCall: TypeAlias = Callable[..., Any]
"""Sync-тело инструмента: аргументы задаёт схема самого тула."""

AsyncCall: TypeAlias = Callable[..., Awaitable[Any]]
"""Async-тело инструмента."""


class CallHooks:
    """Крючки одной обвязки вокруг вызова тела; база — сквозной проход.

    before возвращает контекст вызова, он же приходит в остальные крючки.
    on_error либо поднимает ошибку дальше, либо возвращает замену результата.
    cleanup выполняется всегда, после after или on_error.
    """

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        return None

    def after(self, ctx: object, result: object) -> object:
        return result

    def on_error(self, ctx: object, error: Exception) -> object:
        raise error

    def cleanup(self, ctx: object) -> None:
        return


class ToolBody:
    """Тела инструментов: обёртка ставится на sync и async вместе."""

    @staticmethod
    def wrap_all(
        tools: Sequence[BaseTool],
        wrap: Callable[[SyncCall, str], SyncCall],
        wrap_async: Callable[[AsyncCall, str], AsyncCall],
    ) -> list[BaseTool]:
        """Оборачивает тела каждого инструмента; порядок списка сохраняется."""
        wrapped: list[BaseTool] = []
        for tool in tools:
            wrapped.append(tool)

            if not isinstance(tool, StructuredTool):
                continue

            if tool.func is not None:
                tool.func = wrap(tool.func, tool.name)

            if tool.coroutine is not None:
                tool.coroutine = wrap_async(tool.coroutine, tool.name)

        return wrapped

    @classmethod
    def hook_all(
        cls, tools: Sequence[BaseTool], hooks: CallHooks
    ) -> list[BaseTool]:
        """Ставит одну обвязку крючками на оба тела каждого инструмента."""

        def wrap(call: SyncCall, name: str) -> SyncCall:
            @wraps(call)
            def guarded(*args: object, **kwargs: object) -> object:
                ctx = hooks.before(name, args, kwargs)
                try:
                    result = call(*args, **kwargs)
                except Exception as e:
                    return hooks.on_error(ctx, e)
                else:
                    return hooks.after(ctx, result)
                finally:
                    hooks.cleanup(ctx)

            return guarded

        def wrap_async(call: AsyncCall, name: str) -> AsyncCall:
            @wraps(call)
            async def guarded(*args: object, **kwargs: object) -> object:
                ctx = hooks.before(name, args, kwargs)
                try:
                    result = await call(*args, **kwargs)
                except Exception as e:
                    return hooks.on_error(ctx, e)
                else:
                    return hooks.after(ctx, result)
                finally:
                    hooks.cleanup(ctx)

            return guarded

        return cls.wrap_all(tools, wrap, wrap_async)
