"""Общие фикстуры для интеграционных тестов агента.

Всё «инфраструктурное» (конфиг, логи, контейнер, scope, сбор событий)
живёт в :class:`boba.infra.AgentHarness`. Тестам остаётся запросить
фикстуру и вызвать ``harness.ask(ws_id, query)``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from boba.domain.agent.events import AgentEvent, GenerationDone, GenerationFailed
from boba.infra import AgentHarness

OutcomeClassifier = Callable[[Iterable[AgentEvent]], str]


@pytest.fixture
def harness() -> AgentHarness:
    """AgentHarness с ``max_iterations=1`` — для smoke-прогонов одного запроса."""
    return AgentHarness(max_iterations=1)


_ERROR_KIND_TO_TAG = {
    "LLMContextLengthError": "failed_context_length",
    "LLMInvalidRequestError": "failed_invalid_request",
    "LLMConnectionError": "failed_connection",
    "LLMTimeoutError": "failed_timeout",
}


def _classify_overflow_outcome(events: Iterable[AgentEvent]) -> str:
    for ev in events:
        if isinstance(ev, GenerationFailed):
            if ev.status_code == 413:
                return "failed_413_payload_too_large"
            return _ERROR_KIND_TO_TAG.get(ev.error_kind, f"failed_{ev.error_kind}")
    for ev in events:
        if isinstance(ev, GenerationDone):
            return "done_trimmed_by_proxy"
    return "no_terminal_event"


@pytest.fixture
def classify_overflow_outcome() -> OutcomeClassifier:
    """Свернуть поток событий агента в один тег исхода перегрузки контекста."""
    return _classify_overflow_outcome
