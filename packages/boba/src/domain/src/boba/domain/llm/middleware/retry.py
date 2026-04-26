"""Retry-middleware LLM-слоя.

Повторяет обращение к inner при RetryableLLMError — но
**только если** до момента ошибки ни одного события не было
сэмичено вверх. Если стрим уже «заговорил» (yield любого токена или
lifecycle-маркера), повтор обернул бы клиенту дубликат начала
ответа — склеивать разорванную генерацию некорректно, пусть ошибка
летит на границу.

Политика:

- первая попытка (attempt=0) — молчит (happy path);
- перед каждой повторной попыткой (attempt >= 1) эмитится
  LLMRetryAttempt с причиной и HTTP-статусом упавшей;
- ctx.attempt инкрементируется согласно номеру попытки;
- PermanentLLMError (и любое не-RetryableLLMError
  исключение) пропускается наружу без обработки;
- между попытками — пауза delay_seconds через инжектируемый
  sleep (в тестах подменяется на no-op).

Почему тут нет exponential backoff / jitter. На S3 — простейший
линейный backoff. Когда появятся rate-limit-paвный поток запросов
/ провайдеры со строгими retry-after hints — политика выделится в
отдельный RetryPolicy объект, а middleware станет его
исполнителем. Пока не усложняем.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

from boba.domain.core.patterns import StreamSource
from boba.domain.llm.errors import RetryableLLMError
from boba.domain.llm.events import LLMEvent, LLMRetryAttempt
from boba.domain.llm.models import LLMContext

logger = logging.getLogger(__name__)


class RetryMiddleware(StreamSource[LLMContext, LLMEvent]):
    """Повторяет inner до max_attempts раз на
    RetryableLLMError, пока стрим не начал выдавать события.
    """

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
                # last_exc гарантированно не None: в цикл с attempt > 0 попали
                # только через ветку `except RetryableLLMError` ниже.
                if last_exc is None:  # pragma: no cover — инвариант
                    raise RuntimeError("retry invariant broken: last_exc is None")
                ctx.attempt = attempt
                yield LLMRetryAttempt(
                    request_id=ctx.request_id,
                    attempt=attempt,
                    reason=type(last_exc).__name__,
                    status_code=getattr(last_exc, "status_code", None),
                )
                if self._delay_seconds > 0:
                    self._sleep(self._delay_seconds)
                # Сбрасываем state stateful-стадий внутри inner (декодеры,
                # счётчики): предыдущая попытка могла успеть что-то накопить,
                # даже если ничего не вышло наружу.
                self._inner.reset()

            yielded = False
            try:
                for event in self._inner.stream(ctx):
                    yielded = True
                    yield event
                return
            except RetryableLLMError as e:
                if yielded:
                    # Стрим уже «заговорил» — повтор дал бы дубликат.
                    raise
                logger.warning(
                    "LLM attempt %d/%d failed, will retry: %s: %s",
                    attempt + 1,
                    self._max_attempts,
                    type(e).__name__,
                    e,
                )
                last_exc = e

        # Исчерпали попытки — пробрасываем последнюю ошибку.
        if last_exc is None:  # pragma: no cover — инвариант
            raise RuntimeError("retry invariant broken: no attempts made")
        raise last_exc
