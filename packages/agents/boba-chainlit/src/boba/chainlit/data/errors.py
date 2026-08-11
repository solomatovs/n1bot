"""Контракт ошибок слоя данных: наружу выходят только эти типы.

Ошибки: DataUnavailableError — хранилище недоступно или ответило некорректно;
DataRejectedError — запрос слою данных невозможен на этих данных; DataBrokenError —
нарушен инвариант самого слоя.

Всё чужое (psycopg, файловая система, журнал потоков) упаковывается здесь и
никогда не доходит до вызывающего в исходном виде.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from typing_extensions import ParamSpec

from boba.chainlit.domain.errors import (
    BaseError,
    HttpErrorMessage,
    ViewErrorMessage,
)

__all__ = [
    "DataBrokenError",
    "DataLayerError",
    "DataRejectedError",
    "DataUnavailableError",
    "data_boundary",
]

_P = ParamSpec("_P")
_R = TypeVar("_R")


class DataLayerError(BaseError):
    """База ошибок слоя данных: у каждой есть операция, на которой он упал."""

    STATUS: int = 500
    """Код ответа, если ошибка дошла до HTTP-границы."""

    USER_TEXT: str = "Storage is not available"
    """Что видит пользователь: детали остаются в логе."""

    def __init__(self, operation: str, detail: str) -> None:
        super().__init__(f"{operation}: {detail}")
        self.operation = operation
        self.detail = detail

    def view_message(self) -> ViewErrorMessage:
        return ViewErrorMessage(content=self.USER_TEXT)

    def http_message(self) -> HttpErrorMessage:
        return HttpErrorMessage(status_code=self.STATUS, content=str(self))


class DataUnavailableError(DataLayerError):
    """Хранилище недоступно или ответило не тем: база, диск, журнал."""

    STATUS: int = 503
    USER_TEXT: str = "Storage is not available"


class DataRejectedError(DataLayerError):
    """Операция невозможна на этих данных: нет треда, нет автора, нет ключа."""

    STATUS: int = 404
    USER_TEXT: str = "The requested data is not found"


class DataBrokenError(DataLayerError):
    """Слой данных нарушил собственный инвариант — это наша ошибка."""

    STATUS: int = 500
    USER_TEXT: str = "Internal storage error"


def data_boundary(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Граница слоя: наружу уходит только DataLayerError.

    Свои ошибки пропускаются как есть, чужие пакуются в DataBrokenError с
    сохранением причины.
    """

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await fn(*args, **kwargs)
        except DataLayerError:
            raise
        except Exception as exc:
            raise DataBrokenError(fn.__qualname__, str(exc)) from exc

    return wrapper
