"""Ошибки callback'ов уходят вкладке сами: chainlit гасит их исключения
молча. Это сбои самого чата вне хода — в истории агента их нет, поэтому они
показываются всплывающим уведомлением, а не сообщением ленты."""

import functools
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from chainlit.context import context

from boba.identity.errors import BaseError, FailureReport
from boba.identity.session import LogLine

__all__ = ["ToastLevel", "chainlit_error_ctx_handler", "show_error"]


class ToastLevel(StrEnum):
    """Вид всплывающего уведомления chainlit (`send_toast`)."""

    ERROR = "error"


async def show_error(content: str) -> None:
    """Сбой вкладке всплывающим уведомлением: raise из фоновых тасок
    chainlit не доходит до UI."""
    logger = logging.getLogger(__name__)
    logger.error("%s", LogLine.safe(content))
    try:
        await context.emitter.send_toast(content, ToastLevel.ERROR.value)
    except Exception as exc:
        # сбой доставки не должен подменять собой исходную ошибку у вызвавшего
        logger.exception(
            "failed to show the error %r in the tab: %s", LogLine.safe(content), exc
        )


def chainlit_error_ctx_handler(fn: Callable) -> Callable:

    logger = logging.getLogger(__name__)

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except BaseError as e:
            logger.exception("%s", LogLine.safe(FailureReport.of(e).log))
            if m := e.view_message():
                await show_error(m.content)
        except Exception as e:
            # один разбор на журнал и вкладку: формулировка совпадает дословно
            report = FailureReport.of(e)
            logger.exception("%s", LogLine.safe(report.log))
            if report.view:
                await show_error(report.view)

    return wrapper
