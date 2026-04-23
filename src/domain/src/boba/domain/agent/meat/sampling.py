"""Middleware, применяющая дефолтные sampling-параметры к запросу."""

from __future__ import annotations

from collections.abc import Iterator

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext, LLMRequestDefaults
from boba.domain.core.patterns import StreamSource


class SamplingMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Заливает дефолтные sampling-параметры и ``parallel_tool_calls`` в
    ``ctx.llm_builder`` на каждой итерации. Значения приходят из
    :class:`LLMRequestDefaults` (его загружает
    :class:`~boba.infra.config.ConfigLoader` из TOML/env).

    :class:`LLMRequestFactory` читает эти слоты при сборке
    :class:`LLMRequest`. Отключение middleware через DI возвращает
    провайдерские дефолты (ничего не отправляется в kwargs).
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        defaults: LLMRequestDefaults,
    ) -> None:
        self._inner = inner
        self._defaults = defaults

    def name(self) -> str:
        return "Sampling"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.llm_request.sampling = self._defaults.sampling
        ctx.llm_request.tools.parallel_tool_calls = self._defaults.parallel_tool_calls
        yield from self._inner.stream(ctx)
