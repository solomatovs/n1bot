"""Middleware, применяющая дефолтные sampling-параметры к запросу."""

from __future__ import annotations

from collections.abc import Iterator

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext, LLMRequestDefaults
from boba.domain.core.patterns import StreamSource


class SamplingMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    sampling-параметры из config
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
        yield from self._inner.stream(ctx)
