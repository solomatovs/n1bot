"""Интеграционный тест: HTTP-тело запроса больше лимита прокси.

Отправляем user-prompt ~60 MB — заведомо больше ``client_max_body_size``
"""

from __future__ import annotations

from conftest import OutcomeClassifier

from boba.domain.core.workspace import WorkspaceId
from boba.infra import AgentHarness

_WORKSPACE_ID = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000003")
_PHRASE = "ААА БББ ВВВ "
_REPEAT = 5_000_000  # ~60 MB

_ACCEPTABLE = frozenset(
    {
        "failed_413_payload_too_large",
        "failed_invalid_request",
        "failed_connection",
        "failed_timeout",
    }
)


def test_context_payload_too_large(
    harness: AgentHarness,
    classify_overflow_outcome: OutcomeClassifier,
) -> None:
    query = _PHRASE * _REPEAT + "теперь ответь одним словом: ок"
    events = harness.ask(_WORKSPACE_ID, query)
    outcome = classify_overflow_outcome(events)
    print(f"[test] outcome={outcome}")  # noqa: T201
    assert outcome in _ACCEPTABLE, (
        f"unexpected outcome={outcome}; "
        f"acceptable={sorted(_ACCEPTABLE)}; "
        f"events={[type(e).__name__ for e in events]}"
    )
