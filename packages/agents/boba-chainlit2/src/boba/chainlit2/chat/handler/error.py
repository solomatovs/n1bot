"""Обработчик ошибок chainlit-callback'ов.

Chainlit гасит исключения callback'ов молча, поэтому сбой показываем
пользователю сами: любое исключение уходит в чат сообщением об ошибке.
"""

import functools
import logging
from collections.abc import Callable
from typing import Any

import chainlit as cl

from boba.auth.errors import BaseError


def chainlit_error_ctx_handler(fn: Callable) -> Callable:

    logger = logging.getLogger("chainlit_handler")

    @staticmethod
    async def handle(e: BaseError):
        logger.exception(str(e))
        if m := e.view_message():
            await show(m.content, m.author, m.fail_on_persist_error)

    @staticmethod
    async def show(content: str, author: str, fail_on_persist_error: bool = False):
        message = cl.ErrorMessage(
            author=author,
            content=content,
            fail_on_persist_error=fail_on_persist_error,
        )
        message.parent_id = None
        await message.send()

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BaseError as e:
            await handle(e)
        except Exception as e:
            logger.exception(str(e))
            await show(str(e) or type(e).__name__, "Error")

    return wrapper
