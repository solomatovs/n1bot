"""Generic-протокол наблюдения LLM-вызовов на wire-слое."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

TRequest = TypeVar("TRequest")
TChunk = TypeVar("TChunk")


class RequestOutcomeKind(Enum):
    """Дискриминатор RequestOutcome."""

    OK = "ok"
    CANCELLED = "cancelled"
    RAISED = "raised"


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """Исход запроса LLM на wire-слое; exception_name только при RAISED."""

    kind: RequestOutcomeKind
    exception_name: str | None = None

    def __post_init__(self) -> None:
        is_raised = self.kind is RequestOutcomeKind.RAISED
        if is_raised and not self.exception_name:
            raise ValueError("RAISED requires exception_name")
        if not is_raised and self.exception_name is not None:
            raise ValueError(f"{self.kind.name} must not carry exception_name")

    @classmethod
    def ok(cls) -> RequestOutcome:
        return cls(RequestOutcomeKind.OK)

    @classmethod
    def cancelled(cls) -> RequestOutcome:
        return cls(RequestOutcomeKind.CANCELLED)

    @classmethod
    def raised(cls, exc: BaseException) -> RequestOutcome:
        return cls(RequestOutcomeKind.RAISED, type(exc).__name__)

    def label(self) -> str:
        """Человеко-читаемая метка: ok / cancelled / raised:<Exc>."""
        if self.kind is RequestOutcomeKind.RAISED:
            return f"{self.kind.value}:{self.exception_name}"
        return self.kind.value


class LLMRequestObserver(ABC, Generic[TRequest, TChunk]):
    """Наблюдатель сырого LLM-вызова на границе адаптера."""

    @abstractmethod
    def on_request(self, request: TRequest) -> None:
        """Вызывается один раз перед отправкой запроса к LLM."""
        ...

    @abstractmethod
    def on_response_chunk(self, chunk: TChunk) -> None:
        """Вызывается на каждый chunk потока ответа."""
        ...

    @abstractmethod
    def on_request_end(self, outcome: RequestOutcome) -> None:
        """Вызывается ровно один раз по завершении потока (любой исход)."""
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


class CompositeLLMRequestObserver(LLMRequestObserver[TRequest, TChunk]):
    """Fan-out из нескольких LLMRequestObserver в порядке регистрации."""

    def __init__(
        self, observers: Sequence[LLMRequestObserver[TRequest, TChunk]]
    ) -> None:
        self._observers = observers

    def on_request(self, request: TRequest) -> None:
        for o in self._observers:
            o.on_request(request)

    def on_response_chunk(self, chunk: TChunk) -> None:
        for o in self._observers:
            o.on_response_chunk(chunk)

    def on_request_end(self, outcome: RequestOutcome) -> None:
        for o in self._observers:
            o.on_request_end(outcome)

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
