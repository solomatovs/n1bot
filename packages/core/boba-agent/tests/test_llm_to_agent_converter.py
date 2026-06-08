"""Контракт LLMToAgentConverter: LLM-события -> агент-события."""

from __future__ import annotations

from boba.agent.events import LLMUsageReported
from boba.agent.middleware.llm import LLMToAgentConverter
from boba.llm.events import LLMUsageMessage
from boba.llm.models import Usage, new_request_id


def test_usage_message_maps_to_usage_reported() -> None:
    """LLMUsageMessage -> LLMUsageReported (DiagnosticEvent) с теми же числами."""
    rid = new_request_id()
    event = LLMUsageMessage(
        request_id=rid,
        usage=Usage(
            prompt_tokens=10,
            completion_tokens=3,
            total_tokens=13,
            cost=0.0042,
        ),
    )

    out = list(LLMToAgentConverter().convert(event))

    assert len(out) == 1
    reported = out[0]
    assert isinstance(reported, LLMUsageReported)
    assert reported.request_id == rid
    assert reported.topic == "llm.usage"
    assert reported.total_tokens == 13
    assert reported.prompt_tokens == 10
    assert reported.completion_tokens == 3
    assert reported.cost == 0.0042
    # headline/details выведены из токенов
    assert "13" in reported.headline
    assert reported.details["total_tokens"] == "13"
