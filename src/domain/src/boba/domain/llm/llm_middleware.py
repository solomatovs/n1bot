"""LLM middleware — чистая доменная логика, без внешних зависимостей."""

from __future__ import annotations

import logging
import time
from typing import Iterator

from boba.domain.llm.llm import LLMDelta, LLMRequest
from boba.domain.core.stream import StreamMiddleware, StreamSource

logger = logging.getLogger(__name__)


class LoggingLLMMiddleware(StreamMiddleware[LLMRequest, LLMDelta]):
    """Логирует запрос, количество чанков и время генерации."""

    def name(self) -> str:
        return "LoggingLLM"

    def produce(self, ctx: LLMRequest) -> Iterator[LLMDelta]:
        logger.info("LLM request: model=%s", ctx.model)
        start = time.monotonic()
        chunks = 0

        for delta in self._next.produce(ctx):
            chunks += 1
            yield delta

        elapsed = time.monotonic() - start
        logger.info("LLM done: %d chunks in %.2fs", chunks, elapsed)


class RetryLLMMiddleware(StreamMiddleware[LLMRequest, LLMDelta]):
    """Повторяет запрос при ошибке до max_retries раз."""

    def __init__(self, next: StreamSource[LLMRequest, LLMDelta], max_retries: int = 3) -> None:
        super().__init__(next)
        self._max_retries = max_retries

    def name(self) -> str:
        return "RetryLLM"

    def produce(self, ctx: LLMRequest) -> Iterator[LLMDelta]:
        for attempt in range(self._max_retries):
            try:
                yield from self._next.produce(ctx)
                return
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                logger.warning(
                    "LLM attempt %d/%d failed, retrying",
                    attempt + 1,
                    self._max_retries,
                )
