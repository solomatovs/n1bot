import functools
import logging
from collections.abc import Callable
from typing import Any

import chainlit as cl

from boba.chainlit2.errors import BaseError, InternalServiceError


def chainlit_error_ctx_handler(fn: Callable) -> Callable:
    """Ловит ошибку для chainlit callback которые существуют с контекстом"""

    logger = logging.getLogger("chainlit_error_ctx_handler")

    @staticmethod
    async def handle(e: BaseError):
        # логируем ошибку
        e.log_message(logger)

        # показываем ошибку пользователю
        if m := e.view_message():
            await cl.ErrorMessage(
                author=m.author,
                content=m.content,
                fail_on_persist_error=m.fail_on_persist_error,
            ).send()

        # записываем сообщение в историю, которая доступна
        # при сборке следующего turn'а
        if _history_message := e.history_message():
            # пока что нет сервиса для ведения истории
            pass

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BaseError as e:
            await handle(e)
        except Exception as e:
            exc = InternalServiceError(
                internal_detail=str(e),
                user_detail=None,
            )
            await handle(exc)

    return wrapper


def chainlit_error_handler(fn: Callable) -> Callable:
    """Ловит ошибку для chainlit callback которые существуют без контекста"""

    logger = logging.getLogger("chainlit_error_handler")

    @staticmethod
    async def handle(e: BaseError):
        # логируем ошибку
        e.log_message(logger)

        # показываем ошибку пользователю
        if m := e.view_message():
            await cl.ErrorMessage(
                author=m.author,
                content=m.content,
                fail_on_persist_error=m.fail_on_persist_error,
            ).send()

        # записываем сообщение в историю, которая доступна
        # при сборке следующего turn'а
        if _history_message := e.history_message():
            # пока что нет сервиса для ведения истории
            pass

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BaseError as e:
            await handle(e)
        except Exception as e:
            exc = InternalServiceError(
                internal_detail=str(e),
                user_detail=None,
            )
            await handle(exc)

        # любая ошибка в auth = отказ; chainlit на None отдаёт 401
        return None

    return wrapper
