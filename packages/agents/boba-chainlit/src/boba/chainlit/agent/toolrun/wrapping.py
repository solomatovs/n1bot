"""Постановка обёрток на тело langchain-инструмента.

Обвязки (логи, права, отмена, тап потока, ошибки) отличаются только крючками
вокруг вызова, а способ постановки один: у инструмента два тела — sync-функция
и корутина, — и обёртка должна лечь на оба. Sync/async-близнецы живут здесь
один раз; обвязка описывает только свои крючки CallHooks — они синхронные и
работают в обоих телах; крючок before_async — для обвязок, которым нужно
дождаться значения (билет, соединения) и которые в sync-теле отказывают.
Инструменты приложения строит декоратор `@tool`, то есть это StructuredTool;
тула другого класса обвязка не касается. Схему инструмента правит ToolSchema —
единственное место, где поля добавляются и снимаются.

Ошибки: своих не выпускает; исключения крючков идут наверх как есть.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from functools import wraps
from typing import Any, Generic, TypeAlias, TypeVar

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, create_model

__all__ = [
    "AsyncCall",
    "CallHooks",
    "SyncCall",
    "ToolAsyncBody",
    "ToolBody",
    "ToolSchema",
]


class ToolAsyncBody:
    """Корутина для инструмента с одним sync-телом: вызов уходит в поток.

    Без корутины StructuredTool.ainvoke гонит в поток весь sync-вызов вместе
    с колбэками, и async-трасер ленты исполняется там в чужом event loop —
    шаги ленты не доходят до базы. С корутиной колбэки остаются в loop
    приложения, в поток уходит только тело. Ставится последней, поверх всех
    обвязок: тело к этому моменту уже обёрнуто, корутине обвязка не нужна.
    """

    @classmethod
    def ensure_all(cls, tools: Sequence[BaseTool]) -> None:
        for tool in tools:
            cls._ensure(tool)

    @classmethod
    def _ensure(cls, tool: BaseTool) -> None:
        if not isinstance(tool, StructuredTool):
            return

        if tool.coroutine is not None:
            return

        func = tool.func
        if func is None:
            return

        tool.coroutine = cls._threaded(func)

    @staticmethod
    def _threaded(func: SyncCall) -> AsyncCall:
        @wraps(func)
        async def body(*args: object, **kwargs: object) -> object:
            return await asyncio.to_thread(func, *args, **kwargs)

        return body


CallCtx = TypeVar("CallCtx")
"""Контекст вызова, который обвязка заводит в before и читает в остальном."""

SyncCall: TypeAlias = Callable[..., Any]
"""Sync-тело инструмента: аргументы задаёт схема самого тула."""

AsyncCall: TypeAlias = Callable[..., Awaitable[Any]]
"""Async-тело инструмента."""


class CallHooks(Generic[CallCtx]):
    """Крючки одной обвязки вокруг вызова тела; база — сквозной проход.

    before возвращает контекст вызова, он же приходит в остальные крючки —
    его тип обвязка объявляет параметром, поэтому приведения типа внутри
    крючков не нужны. on_error либо поднимает ошибку дальше, либо возвращает
    замену результата; cleanup выполняется всегда, после after или on_error.
    """

    def before(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> CallCtx:
        raise NotImplementedError

    async def before_async(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> CallCtx:
        """Крючок async-тела; по умолчанию — тот же before."""
        return self.before(name, args, kwargs)

    def after(self, ctx: CallCtx, result: object) -> object:
        return result

    def on_error(self, ctx: CallCtx, error: Exception) -> object:
        raise error

    def cleanup(self, ctx: CallCtx) -> None:
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
        cls, tools: Sequence[BaseTool], hooks: CallHooks[Any]
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
                ctx = await hooks.before_async(name, args, kwargs)
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


class ToolSchema:
    """Схема аргументов StructuredTool: чтение с мягким guard'ом и пересборка."""

    @staticmethod
    def of(tool: BaseTool) -> type[BaseModel] | None:
        """Pydantic-схема инструмента; None — не StructuredTool или схема иная."""
        if not isinstance(tool, StructuredTool):
            return None

        schema = tool.args_schema
        if not isinstance(schema, type):
            return None

        if not issubclass(schema, BaseModel):
            return None

        return schema

    @staticmethod
    def rebuild(
        schema: type[BaseModel],
        add: Mapping[str, tuple[Any, Any]],
        drop: Iterable[str],
    ) -> type[BaseModel]:
        """Та же схема с добавленными полями (аннотация, дефолт) и без снятых."""
        dropped = frozenset(drop)

        fields: dict[str, Any] = {}
        for name, info in schema.model_fields.items():
            if name in dropped:
                continue

            fields[name] = (info.annotation, info)

        for name, declared in add.items():
            fields[name] = declared

        return create_model(schema.__name__, **fields)
