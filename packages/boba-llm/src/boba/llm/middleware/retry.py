"""Retry-middleware LLM-слоя для RetryableLLMError."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from boba.llm.errors import RetryableLLMError
from boba.llm.events import LLMEvent, LLMRetryAttempt
from boba.llm.models import LLMContext
from boba.patterns import StreamSource

__all__ = ["RetryMiddleware"]

logger = logging.getLogger(__name__)


class RetryMiddleware(StreamSource[LLMContext, LLMEvent]):
    """Повторяет inner до max_attempts на RetryableLLMError (пока ничего не yield)."""

    def __init__(
        self,
        inner: StreamSource[LLMContext, LLMEvent],
        max_attempts: int = 3,
        delay_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._inner = inner
        self._max_attempts = max_attempts
        self._delay_seconds = delay_seconds
        self._sleep = sleep

    def name(self) -> str:
        return f"Retry({self._inner.name()})"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        last_exc: RetryableLLMError | None = None

        for attempt in range(self._max_attempts):
            if attempt > 0:
                if last_exc is None:  # pragma: no cover — инвариант
                    raise RuntimeError("retry invariant broken: last_exc is None")

                yield LLMRetryAttempt(
                    request_id=ctx.request_id,
                    attempt=attempt,
                    reason=type(last_exc).__name__,
                    status_code=getattr(last_exc, "status_code", None),
                )
                if self._delay_seconds > 0:
                    self._sleep(self._delay_seconds)
                self._inner.reset()

            yielded = False
            try:
                for event in self._inner.stream(ctx):
                    yielded = True
                    yield event
                return
            except RetryableLLMError as e:
                if yielded:
                    raise
                logger.warning(
                    "LLM attempt %d/%d failed, will retry: %s: %s",
                    attempt + 1,
                    self._max_attempts,
                    type(e).__name__,
                    e,
                )
                last_exc = e

        if last_exc is None:  # pragma: no cover — инвариант
            raise RuntimeError("retry invariant broken: no attempts made")
        raise last_exc
