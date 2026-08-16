"""Ошибки callback'ов уходят в чат сами: chainlit гасит их исключения молча."""

import functools
import logging
from collections.abc import Callable
from typing import Any

import chainlit as cl
from boba.chainlit.domain.errors import BaseError, FailureReport

__all__ = ["chainlit_error_ctx_handler", "show_error"]


async def show_error(
    content: str,
    author: str = "Error",
    fail_on_persist_error: bool = False,
) -> None:
    """Сбой в чат сообщением: raise из фоновых тасок chainlit не доходит до UI."""
    logger = logging.getLogger("chainlit_handler")
    logger.error(content)
    message = cl.ErrorMessage(
        author=author,
        content=content,
        fail_on_persist_error=fail_on_persist_error,
    )
    message.parent_id = None
    try:
        await message.send()
    except Exception:
        # сбой доставки не должен подменять собой исходную ошибку у вызвавшего
        logger.exception("failed to show the error in chat")


def chainlit_error_ctx_handler(fn: Callable) -> Callable:

    logger = logging.getLogger("chainlit_handler")

    @staticmethod
    async def handle(e: BaseError):
        logger.exception(FailureReport.of(e).log)
        if m := e.view_message():
            await show(m.content, m.author, m.fail_on_persist_error)

    @staticmethod
    async def show(content: str, author: str, fail_on_persist_error: bool = False):
        await show_error(content, author, fail_on_persist_error)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BaseError as e:
            await handle(e)
        except Exception as e:
            # один разбор на журнал и чат: формулировка совпадает дословно
            report = FailureReport.of(e)
            logger.exception(report.log)
            if report.view:
                await show(report.view, "Error")

    return wrapper
