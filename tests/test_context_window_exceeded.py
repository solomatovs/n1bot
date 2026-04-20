"""Интеграционный тест: запрос превышает context window модели.

Отправляем user-prompt ~1.2 MB (~300–500K cyrillic-токенов) — заведомо
больше окна qwen3. Конкретный исход зависит от настроек прокси перед
LLM (обычно LiteLLM):

* **failed_context_length** — провайдер вернул HTTP 400 с
  ``context_length_exceeded``; адаптер смапил в
  :class:`~boba.domain.agent.errors.LLMContextLengthError`. Желаемый
  исход — корректная диагностика от самой модели.
* **failed_invalid_request** — HTTP 400 по другой причине (иной формат
  ошибки у конкретного провайдера, всё равно не-``Retryable``).
* **done_trimmed_by_proxy** — LiteLLM с ``context_window_fallback`` /
  ``max_input_tokens`` молча обрезал prompt; запрос принят, модель
  ответила по урезанному контексту. Это НЕ баг в нашем коде — настройка
  прокси. Чтобы увидеть 400: отключить auto-trim на прокси.

Любой другой исход (`no_terminal_event`, неожиданный `failed_*`) —
сигнал регрессии: либо адаптер маппит ошибку неправильно, либо цикл
агента не останавливается на терминальном событии.
"""

from __future__ import annotations

from conftest import OutcomeClassifier

from boba.domain.core.workspace import WorkspaceId
from boba.infra import AgentHarness

_WORKSPACE_ID = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000002")
_PHRASE = "ААА БББ ВВВ "
_REPEAT = 100_000  # ~1.2 MB, 300–500K cyrillic tokens

_ACCEPTABLE = frozenset(
    {
        "failed_context_length",
        "failed_invalid_request",
        "done_trimmed_by_proxy",
    }
)


def test_context_window_exceeded(
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
