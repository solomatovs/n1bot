"""Generic-протокол наблюдения LLM-вызовов на wire-слое.

Параметризуется типом запроса (то, что уходит провайдеру) и типом
chunk-а ответного стрима. Конкретные адаптеры (OpenAI Chat Completions,
Anthropic Messages, Gemini и т.п.) биндят эти параметры под свои
SDK-типы — например, наследуясь от
LLMRequestObserver[dict[str, Any], ChatCompletionChunk] в
boba.adapter.openai.observer.

Контракт жизненного цикла:
- on_request — ровно один раз перед HTTP-вызовом, с уже собранным
  payload запроса;
- on_response_chunk — ноль или больше раз, по одному chunk-у потока;
- on_request_end — ровно один раз в любом исходе (OK / CANCELLED /
  RAISED), даже при отмене или исключении.

RequestOutcome — дискриминированный итог запроса; инвариант
exception_name проверяется в __post_init__, нелегальные
комбинации непредставимы.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
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
    """Исход запроса LLM на wire-слое.

    Инвариант: exception_name задан тогда и только тогда, когда
    kind is RequestOutcomeKind.RAISED. Остальные комбинации
    отвергаются в __post_init__ — нелегальные состояния
    непредставимы.
    """

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
    """Наблюдатель сырого LLM-вызова на границе адаптера.

    Generic-параметры:
    - TRequest — тип payload-а запроса в формате конкретного провайдера
      (для OpenAI — dict[str, Any] с kwargs chat.completions.create,
      для Anthropic — MessageCreateParams и т.д.);
    - TChunk — тип элемента ответного стрима (ChatCompletionChunk,
      MessageStreamEvent, ...).

    Биндинги под конкретные SDK живут в адаптерах — например,
    наблюдатели OpenAI Chat Completions в boba.adapter.openai.observer
    наследуются от LLMRequestObserver[dict[str, Any], ChatCompletionChunk].
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
    def on_request_end(self, outcome: RequestOutcome) -> None:
        """Вызывается ровно один раз по завершении потока (любой исход)."""
        ...


class CompositeLLMRequestObserver(LLMRequestObserver[TRequest, TChunk]):
    """Fan-out из нескольких LLMRequestObserver — вызывает каждого
    последовательно в порядке регистрации.

    Исключение в любом наблюдателе прерывает обход — оставшиеся
    наблюдатели не получат текущее событие. Если нужна
    exception-tolerant обвязка — заверните конкретного наблюдателя в
    декоратор с локальным try/except.
    """

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
