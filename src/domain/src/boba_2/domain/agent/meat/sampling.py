"""SamplingMiddleware — применяет дефолтные sampling-параметры к запросу.

Простая стадия внутри loop: на каждой итерации проставляет
:class:`SamplingParams` в ``ctx.request.sampling``. Ставится после
:class:`IterationCounterMiddleware` (чтобы request уже был свежим),
но до ``LLMInvoke`` — чтобы снапшот запроса собирался с учётом
дефолтов.

Если upstream-middleware уже задал свои sampling-поля (temperature
и т.п.), :class:`SamplingParams` **заменяет** их целиком — это
осознанный выбор контракта: дефолты всегда «последнее слово».
Когда понадобится тонкий override (заменить только part), появится
отдельная стадия ``SamplingOverride``.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.patterns import StreamSource
from boba_2.domain.agent.events import AgentEvent
from boba_2.domain.agent.models import AgentContext
from boba_2.domain.llm.models import SamplingParams


class SamplingMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Sampling-параметры из конфига → ``ctx.request.sampling``."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        sampling: SamplingParams,
    ) -> None:
        self._inner = inner
        self._sampling = sampling

    def name(self) -> str:
        return "Sampling"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        ctx.request.sampling = self._sampling
        yield from self._inner.stream(ctx)
