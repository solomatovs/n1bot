"""Оркестратор LLM-слоя — :class:`LLMAgent`.

Связка ``source → sink`` на границе LLM-слоя, зеркальная
:class:`~boba_2.domain.agent.meat.agent.Agent` в агент-слое.
``run(request, request_id)`` создаёт :class:`LLMContext` и
пропускает готовую composed-цепочку LLM-middleware в
sink-потребитель. Retry/cache/rate-limit-обёртки навешиваются
снаружи (в контейнере), чтобы LLMAgent оставался тонким
«source/sink-драйвером» без знания о внутренностях цепочки.

Используется там, где LLM-слой нужен самостоятельно — без
обёртки агент-петли (standalone LLM-run, интеграционные тесты
LLM-цепочки). Агент-слой потребляет LLM-события через
:class:`~boba_2.domain.agent.meat.llm_invoke.LLMInvokeMiddleware`,
которому передаётся голая цепочка :class:`StreamSource`, а не
LLMAgent: события должны протечь через трансляцию в
:class:`AgentEvent`, а не осесть в terminal-sink'е LLM-слоя.
"""

from __future__ import annotations

from boba.domain.core.patterns import StreamSink, StreamSource
from boba_2.domain.llm.events import LLMEvent
from boba_2.domain.llm.models import LLMContext, LLMRequest, RequestId


class LLMAgent:
    """Тонкий оркестратор: создаёт контекст, сливает source в sink."""

    def __init__(
        self,
        source: StreamSource[LLMContext, LLMEvent],
        sink: StreamSink[LLMContext, LLMEvent],
    ) -> None:
        self._source = source
        self._sink = sink

    def name(self) -> str:
        return "LLMAgent"

    def run(self, request: LLMRequest, request_id: RequestId) -> None:
        ctx = LLMContext(request=request, request_id=request_id)
        for event in self._source.stream(ctx):
            self._sink.handle(ctx, event)
