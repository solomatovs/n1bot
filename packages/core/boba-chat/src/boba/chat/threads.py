"""Треды чата: строка треда, порты хранилища и владения, контракт ошибок слоя данных.

Ошибки:
DataUnavailableError — хранилище недоступно или ответило некорректно.
DataRejectedError — запрос слою данных невозможен на этих данных.
DataBrokenError — нарушен инвариант самого слоя.

Всё чужое (psycopg, файловая система, журнал потоков) упаковывается на границе
слоя данных и никогда не доходит до вызывающего в исходном виде.
"""

from __future__ import annotations

import functools
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, ParamSpec, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.identity.errors import (
    BaseError,
    HttpErrorMessage,
    ViewErrorMessage,
)

__all__ = [
    "ChatTable",
    "ChatThreads",
    "DataBrokenError",
    "DataLayerError",
    "DataRejectedError",
    "DataUnavailableError",
    "ElementStore",
    "ElementsColumn",
    "FeedbackStore",
    "FeedbacksColumn",
    "StoredElement",
    "StoredFeedback",
    "StoredThread",
    "ThreadOwnership",
    "ThreadStore",
    "ThreadUpsert",
    "ThreadUpserted",
    "ThreadsColumn",
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


class ChatTable(StrEnum):
    """Таблицы схемы чата; users описана в boba.identity.api, остальные — здесь."""

    USERS = "users"
    THREADS = "threads"
    ELEMENTS = "elements"
    FEEDBACKS = "feedbacks"


class ThreadsColumn(StrEnum):
    """Колонки threads."""

    ID = "id"
    CREATED_AT = "created_at"
    NAME = "name"
    USER_ID = "user_id"
    TAGS = "tags"
    META = "meta"


class ElementsColumn(StrEnum):
    """Колонки elements."""

    ID = "id"
    NAME = "name"
    TYPE = "type"
    DISPLAY = "display"
    THREAD_ID = "thread_id"
    FOR_ID = "for_id"
    CHAINLIT_KEY = "chainlit_key"
    SIZE = "size"
    LANGUAGE = "language"
    PAGE = "page"
    PROPS = "props"
    MIME = "mime"


class FeedbacksColumn(StrEnum):
    """Колонки feedbacks."""

    ID = "id"
    FOR_ID = "for_id"
    VALUE = "value"
    THREAD_ID = "thread_id"
    COMMENT = "comment"


class StoredThread(BaseModel):
    """Строка threads как её хранит база; владелец пуст у треда без пользователя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    created_at: datetime
    name: str = ""
    user_id: UUID | None = None
    tags: Sequence[str] = ()
    meta: Mapping[str, Any] = {}


class ThreadUpsert(BaseModel):
    """Что вписать в тред: None у поля — не трогать; meta правится по ключам."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str | None = None
    user_id: UUID | None = None
    tags: Sequence[str] | None = None
    meta_set: Mapping[str, Any] = {}
    meta_del: Sequence[str] = ()


class ThreadUpserted(BaseModel):
    """Итог upsert треда: владелец, имя и создана ли строка только что."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID | None
    name: str
    inserted: bool


class ThreadOwnership(Protocol):
    """Автор треда чата: по нему API проверяет, что тред принадлежит вызывающему."""

    @abstractmethod
    async def get_thread_author(self, thread_id: str) -> str:
        """DataRejectedError — треда нет; DataUnavailableError — хранилище лежит."""


class ThreadStore(Protocol):
    """Строки threads: чтение, upsert, удаление и список пользователя."""

    @abstractmethod
    async def get(self, thread_id: UUID) -> StoredThread | None:
        """None — треда нет."""

    @abstractmethod
    async def upsert(self, change: ThreadUpsert) -> ThreadUpserted: ...

    @abstractmethod
    async def delete(self, thread_id: UUID) -> UUID | None:
        """Владелец удалённого треда; None — треда не было или он без владельца."""

    @abstractmethod
    async def list_of(self, user_id: UUID, limit: int) -> Sequence[StoredThread]:
        """Треды пользователя, новые первыми."""


class ChatThreads(ThreadStore, ThreadOwnership, Protocol):
    """Всё о тредах, что нужно слою данных чата."""


class StoredElement(BaseModel):
    """Строка elements: вложение или артефакт треда, тело живёт в хранилище файлов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    type: str
    display: str
    thread_id: UUID | None = None
    for_id: UUID | None = None
    chainlit_key: str = ""
    size: str = ""
    language: str = ""
    page: int | None = None
    props: Mapping[str, Any] = {}
    mime: str = ""


class StoredFeedback(BaseModel):
    """Строка feedbacks: оценка шага пользователем."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    for_id: UUID
    value: int
    thread_id: UUID | None = None
    comment: str = ""


class ElementStore(Protocol):
    """Строки elements: upsert, чтение, удаление, выборка и очистка треда."""

    @abstractmethod
    async def upsert(self, element: StoredElement) -> None: ...

    @abstractmethod
    async def find(self, element_id: UUID) -> StoredElement | None:
        """None — элемента нет."""

    @abstractmethod
    async def get(self, thread_id: UUID, element_id: UUID) -> StoredElement | None:
        """None — элемента в этом треде нет."""

    @abstractmethod
    async def delete(self, element_id: UUID) -> StoredElement | None:
        """Удалённая строка; None — элемента не было."""

    @abstractmethod
    async def list_of_thread(self, thread_id: UUID) -> Sequence[StoredElement]: ...

    @abstractmethod
    async def delete_of_thread(self, thread_id: UUID) -> None: ...

    @abstractmethod
    async def delete_of_step(self, step_id: UUID) -> None: ...


class FeedbackStore(Protocol):
    """Строки feedbacks: upsert, удаление, выборка и очистка треда."""

    @abstractmethod
    async def upsert(self, feedback: StoredFeedback) -> None: ...

    @abstractmethod
    async def delete(self, feedback_id: UUID) -> StoredFeedback | None:
        """Удалённая строка; None — оценки не было."""

    @abstractmethod
    async def list_of_thread(self, thread_id: UUID) -> Sequence[StoredFeedback]: ...

    @abstractmethod
    async def delete_of_thread(self, thread_id: UUID) -> None: ...

    @abstractmethod
    async def delete_of_step(self, step_id: UUID) -> None: ...


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
            detail = f"unexpected {type(exc).__name__}: {exc}"
            raise DataBrokenError(fn.__qualname__, detail) from exc

    return wrapper
