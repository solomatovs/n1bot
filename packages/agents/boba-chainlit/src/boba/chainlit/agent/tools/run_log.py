"""Логи вызова инструмента и контекст вызова для журнала.

Вокруг вызова пишутся start/ok/failed, и на время работы ставится
`ToolCallContext` — адрес журнала: пользователь и тред сессии плюс id вызова
langchain. Id в аргументы функции сам не приходит, поэтому обвязка добавляет в
схему инструмента поле `InjectedToolCallId` и снимает его перед вызовом:
модель поля не видит, инструмент о нём не знает. Тем, кто объявил такое поле
сам, обвязка его оставляет.

Вне сессии chainlit (CLI, стенд) контекст ставит вызывающий — обвязка чужой
контекст не трогает.

Ошибки:
ToolRunError — схема аргументов инструмента не модель.
ToolRunError — вызов пришёл без id или с неразложимым адресом.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Annotated, ClassVar, cast

from langchain_core.tools import BaseTool, InjectedToolCallId
from pydantic import BaseModel, ValidationError, create_model

from boba.chainlit.domain.session import current_thread_id, current_user_id
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.result import ToolResult, ToolResultBase, render_for_llm

__all__ = ["ToolRunError", "ToolRunLogger"]

logger = logging.getLogger(__name__)


class ToolRunError(RuntimeError):
    """Вызов нельзя обвязать: нет схемы аргументов или адреса вызова."""


class ToolRunLogger:
    """Пишет start/ok/failed вокруг каждого вызова инструмента."""

    ARGS_LIMIT: ClassVar[int] = 500

    PACKED_RESULT: ClassVar[int] = 2
    """Длина кортежа (content, artifact) у tool'ов с content_and_artifact."""

    CALL_ID_ARG: ClassVar[str] = "tool_call_id"
    """Имя поля с id вызова: контракт langchain, его же ждут наши инструменты."""

    @staticmethod
    def guard_all(tools: list[BaseTool]) -> list[BaseTool]:
        for tool in tools:
            func = getattr(tool, "func", None)
            coroutine = cast(
                "Callable[..., Awaitable[object]] | None",
                getattr(tool, "coroutine", None),
            )

            sync_call = callable(func)
            async_call = callable(coroutine)
            # схему трогаем только у обвязанных: снимать поле больше некому
            if not sync_call and not async_call:
                continue

            owned = ToolRunLogger._add_call_id(tool)

            if callable(func):
                tool.func = ToolRunLogger._wrap(func, tool.name, owned)

            if callable(coroutine):
                tool.coroutine = ToolRunLogger._wrap_async(coroutine, tool.name, owned)
        return tools

    @classmethod
    def _add_call_id(cls, tool: BaseTool) -> bool:
        """Поле с id вызова в схему; True — его снимает обвязка, не инструмент."""
        schema = tool.args_schema
        if not isinstance(schema, type):
            raise ToolRunError(f"tool {tool.name!r}: args schema is not a model")

        if not issubclass(schema, BaseModel):
            raise ToolRunError(f"tool {tool.name!r}: args schema is not a model")

        if cls.CALL_ID_ARG in schema.model_fields:
            return False

        # имя поля повторяется литералом: create_model типизирован только так
        tool.args_schema = create_model(
            schema.__name__,
            __base__=schema,
            tool_call_id=(Annotated[str, InjectedToolCallId], ...),
        )

        return True

    @staticmethod
    def _wrap(
        func: Callable[..., object],
        name: str,
        owned: bool,
    ) -> Callable[..., object]:
        @wraps(func)
        def wrapped(*args: object, **kwargs: object) -> object:
            call_id = ToolRunLogger._call_id(kwargs, owned)
            ToolRunLogger._log_start(name, args, kwargs)
            started = time.monotonic()

            with ToolRunLogger._bound(name, call_id):
                try:
                    result = func(*args, **kwargs)
                except Exception as e:
                    ToolRunLogger._log_failure(name, started, e)
                    raise

            ToolRunLogger._log_outcome(name, started, result)
            return result

        return wrapped

    @staticmethod
    def _wrap_async(
        coroutine: Callable[..., Awaitable[object]],
        name: str,
        owned: bool,
    ) -> Callable[..., Awaitable[object]]:
        @wraps(coroutine)
        async def wrapped(*args: object, **kwargs: object) -> object:
            call_id = ToolRunLogger._call_id(kwargs, owned)
            ToolRunLogger._log_start(name, args, kwargs)
            started = time.monotonic()

            with ToolRunLogger._bound(name, call_id):
                try:
                    result = await coroutine(*args, **kwargs)
                except Exception as e:
                    ToolRunLogger._log_failure(name, started, e)
                    raise

            ToolRunLogger._log_outcome(name, started, result)
            return result

        return wrapped

    @classmethod
    def _call_id(cls, kwargs: dict[str, object], owned: bool) -> str:
        """Id вызова из аргументов; добавленное обвязкой поле уходит из вызова."""
        if owned:
            raw = kwargs.pop(cls.CALL_ID_ARG, "")
        else:
            raw = kwargs.get(cls.CALL_ID_ARG, "")

        if not isinstance(raw, str):
            return ""

        return raw

    @classmethod
    @contextmanager
    def _bound(cls, name: str, call_id: str) -> Iterator[None]:
        """Адрес вызова на время работы; вне сессии его ставит вызывающий."""
        user_id = current_user_id()
        if not user_id:
            yield
            return

        thread_id = current_thread_id()
        if not thread_id:
            yield
            return

        context = cls._context(name, user_id, thread_id, call_id)
        token = ToolCallContext.set(context)
        try:
            yield
        finally:
            ToolCallContext.reset(token)

    @staticmethod
    def _context(
        name: str,
        user_id: str,
        thread_id: str,
        call_id: str,
    ) -> ToolCallContext:
        if not call_id:
            raise ToolRunError(f"tool {name!r}: call arrived without a tool call id")

        try:
            return ToolCallContext(
                user_id=user_id,
                thread_id=thread_id,
                call_id=call_id,
                tool=name,
            )
        except ValidationError as exc:
            raise ToolRunError(
                f"tool {name!r}: call address is not addressable: {call_id!r}"
            ) from exc

    @classmethod
    def _log_start(
        cls,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        logger.info("tool[%s]: start args=%s", name, cls._render_args(args, kwargs))

    @staticmethod
    def _log_outcome(name: str, started: float, result: object) -> None:
        """Инструмент мог вернуть отказ вместо исключения — это не «ok»."""
        elapsed = ToolRunLogger._elapsed_ms(started)
        failure = ToolRunLogger._reported_failure(result)
        if failure is None:
            logger.info("tool[%s]: ok in %dms", name, elapsed)
            return
        logger.warning("tool[%s]: failed in %dms: %s", name, elapsed, failure)

    @staticmethod
    def _reported_failure(result: object) -> str | None:
        """Текст отказа из ToolResult; None — инструмент отработал успешно."""
        payload = result
        if isinstance(payload, tuple) and len(payload) == ToolRunLogger.PACKED_RESULT:
            payload = payload[1]
        if not isinstance(payload, ToolResultBase) or payload.ok:
            return None
        return render_for_llm(cast("ToolResult", payload))

    @staticmethod
    def _log_failure(name: str, started: float, error: BaseException) -> None:
        logger.warning(
            "tool[%s]: failed in %dms: %s: %s",
            name,
            ToolRunLogger._elapsed_ms(started),
            type(error).__name__,
            error,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @classmethod
    def _render_args(
        cls,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> str:
        parts: list[str] = []
        for value in args:
            parts.append(repr(value))
        for key, value in kwargs.items():
            parts.append(f"{key}={value!r}")
        text = ", ".join(parts)
        if len(text) > cls.ARGS_LIMIT:
            return text[: cls.ARGS_LIMIT] + "…"
        return text
