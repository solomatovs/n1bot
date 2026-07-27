import functools
import logging
from collections.abc import Callable
from typing import Any

import chainlit as cl

from boba.hermes.errors import BaseError


def chainlit_error_ctx_handler(fn: Callable) -> Callable:
    """Ловит ошибку для chainlit callback которые существуют с контекстом"""

    logger = logging.getLogger("chainlit_handler")

    @staticmethod
    async def handle(e: BaseError):
        # логируем ошибку наследуемую от BaseError
        logger.exception(str(e))
        # записываем сообщение в историю, которая доступна
        # при сборке следующего turn'а
        if _history_message := e.history_message():
            # пока что нет сервиса для ведения истории
            pass

        # показываем ошибку пользователю
        if m := e.view_message():
            await cl.ErrorMessage(
                author=m.author,
                content=m.content,
                fail_on_persist_error=m.fail_on_persist_error,
            ).send()

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BaseError as e:
            await handle(e)

    return wrapper
