"""Generic-протокол наблюдения LLM-вызовов на wire-слое."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import Generic, TypeVar

__all__ = [
    "CompositeLLMRequestObserver",
    "LLMRequestObserver",
]

TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")
TApiError = TypeVar("TApiError", bound=Exception)
THttpError = TypeVar("THttpError", bound=Exception)


class LLMRequestObserver(
    ABC, Generic[TRequest, TChunk, TApiError, THttpError]
):
    """Наблюдатель сырого LLM-вызова на границе адаптера.

    Жизненный цикл:
        on_request →
            [on_http_request → on_http_response → on_response_chunk*] →
            терминал.

    Терминал — ровно один из: on_request_end (OK), on_request_cancel (отмена),
    on_api_exception (ошибка API-протокола), on_http_exception (транспорт).
    Конкретные типы исключений фиксируются провайдером через TApiError/THttpError.
    """

    @abstractmethod
    def on_request(self, request: TRequest) -> None:
        """Вызывается один раз перед отправкой запроса к LLM."""
        ...

    @abstractmethod
    def on_response_chunk(self, chunk: TChunk) -> None:
        """Вызывается на каждый chunk потока ответа."""
        ...

    @abstractmethod
    def on_request_end(self) -> None:
        """Терминал: запрос успешно завершён."""
        ...

    @abstractmethod
    def on_request_cancel(self) -> None:
        """Терминал: потребитель прервал чтение потока."""
        ...

    def on_http_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        """Сырой исходящий HTTP-запрос на транспортном уровне; по умолчанию no-op."""

    def on_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        """Сырой входящий HTTP-ответ (без тела); по умолчанию no-op."""

    def on_api_exception(self, exception: TApiError) -> None:
        """Терминал: ошибка API-протокола (конкретный тип задаёт провайдер);
        наблюдатель не подавляет — вызывающий код re-raise; по умолчанию no-op."""

    def on_http_exception(self, exception: THttpError) -> None:
        """Терминал: транспортный сбой (конкретный тип задаёт провайдер);
        наблюдатель не подавляет — вызывающий код re-raise; по умолчанию no-op."""


class CompositeLLMRequestObserver(
    LLMRequestObserver[TRequest, TChunk, TApiError, THttpError]
):
    """Fan-out из нескольких LLMRequestObserver в порядке регистрации."""

    def __init__(
        self,
        observers: Sequence[
            LLMRequestObserver[TRequest, TChunk, TApiError, THttpError]
        ],
    ) -> None:
        self._observers = observers

    def on_request(self, request: TRequest) -> None:
        for o in self._observers:
            o.on_request(request)

    def on_response_chunk(self, chunk: TChunk) -> None:
        for o in self._observers:
            o.on_response_chunk(chunk)

    def on_request_end(self) -> None:
        for o in self._observers:
            o.on_request_end()

    def on_request_cancel(self) -> None:
        for o in self._observers:
            o.on_request_cancel()

    def on_http_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        for o in self._observers:
            o.on_http_request(method, url, headers, body)

    def on_http_response(
        self,
        status_code: int,
        headers: Mapping[str, str],
    ) -> None:
        for o in self._observers:
            o.on_http_response(status_code, headers)

    def on_api_exception(self, exception: TApiError) -> None:
        for o in self._observers:
            o.on_api_exception(exception)

    def on_http_exception(self, exception: THttpError) -> None:
        for o in self._observers:
            o.on_http_exception(exception)
