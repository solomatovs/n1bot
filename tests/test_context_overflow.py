"""Интеграционный тест: проверка поведения при переполнении контекста.

Отправляет user-prompt заведомо больше context window выбранной модели.
Возможные исходы (каждый полезен диагностически):

1. **Желаемый — HTTP 400 / ``context_length_exceeded``.**
   Адаптер маппит в :class:`LLMContextLengthError` (``PermanentLLMError``),
   error-router эмитит :class:`GenerationFailed` с
   ``error_kind="LLMContextLengthError"``. Retry не сработает —
   ``PermanentLLMError`` не-``Retryable``, цикл остановит
   :class:`StopOnAnyFailure`.

2. **Прокси молча обрезает контекст** (типично для LiteLLM с
   ``context_window_fallback`` / ``max_input_tokens``). Запрос примут,
   модель ответит по урезанному prompt'у — получаем
   :class:`GenerationDone`. Это НЕ проблема нашего кода — это настройка
   прокси. Чтобы увидеть ошибку: отключить auto-trim на LiteLLM ИЛИ
   увеличить ``repeat`` до значения, превышающего физический лимит
   (HTTP тело / proxy buffer).

3. **HTTP 413 Payload Too Large** (если прокси за nginx/envoy с
   ``client_max_body_size``). Мапится в :class:`LLMInvalidRequestError`
   (status=413), либо в :class:`LLMConnectionError`, если соединение
   обрубают на уровне TLS/TCP раньше, чем прилетит HTTP-ответ.

Тест параметризован по ``repeat``. Для каждого значения задан набор
допустимых исходов — конкретный зависит от текущей конфигурации прокси,
поэтому проверяем попадание в множество, а не конкретный исход.

Ручной запуск: ``python test_context_overflow.py [repeat]`` либо через
VS Code launch ``Test Context Overflow``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import replace

import pytest

from boba.app.logging import configure_logging
from boba.domain.agent.events import AgentEvent, GenerationDone, GenerationFailed
from boba.domain.agent.meat import Agent
from boba.domain.agent.models import AgentContext, AgentRequest, RequestId
from boba.domain.core.workspace import UserWorkspaceManager, WorkspaceId
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope

_PHRASE = "ААА БББ ВВВ "
_DEFAULT_REPEAT = 100_000  # ~1.2 MB → 300–500K токенов cyrillic
_OVERSIZED_REPEAT = 5_000_000  # ~60 MB → заведомо больше body-limit прокси
FIXED_WORKSPACE_ID = WorkspaceId.from_wire("00000000-0000-0000-0000-000000000002")


def _build_huge_query(repeat: int) -> str:
    return _PHRASE * repeat + "теперь ответь одним словом: ок"


def _run(repeat: int) -> list[AgentEvent]:
    """Прогнать агент с огромным запросом и вернуть все события цикла."""
    query = _build_huge_query(repeat)
    print(  # noqa: T201
        f"[test] repeat={repeat}, query length: {len(query)} chars "
        f"(грубая оценка ≈ {len(query)}–{len(query) * 2} токенов cyrillic)"
    )

    loader = ConfigLoader()
    app_config = loader.load_app()
    configure_logging(app_config.log_level)
    agent_config = replace(loader.load_agent(), max_iterations=1)
    llm_defaults = loader.load_llm_defaults()
    container = create_container(app_config, agent_config, llm_defaults)

    manager = container.get(UserWorkspaceManager)
    storage = manager.get_or_create(FIXED_WORKSPACE_ID)

    with request_scope(container, storage.workspace_id) as req:
        agent = req.get(Agent)
        request = AgentRequest(
            query=query,
            model=app_config.llm.model,
            workspace_id=storage.workspace_id,
            request_id=RequestId.new(),
        )
        ctx = AgentContext(request=request, config=agent_config)
        events: list[AgentEvent] = []
        # Инлайн цикла Agent.run — нам нужны сами события для ассертов,
        # но реальные sink'и (console/history) тоже должны отработать.
        for event in agent._source.stream(ctx):
            events.append(event)
            agent._sink.handle(ctx, event)
        return events


_ERROR_KIND_TO_TAG = {
    "LLMContextLengthError": "failed_context_length",
    "LLMInvalidRequestError": "failed_invalid_request",
    "LLMConnectionError": "failed_connection",
    "LLMTimeoutError": "failed_timeout",
}


def _classify(events: Iterable[AgentEvent]) -> str:
    """Свернуть поток событий в один диагностический тег исхода."""
    for ev in events:
        if isinstance(ev, GenerationFailed):
            if ev.status_code == 413:
                return "failed_413_payload_too_large"
            return _ERROR_KIND_TO_TAG.get(ev.error_kind, f"failed_{ev.error_kind}")
    for ev in events:
        if isinstance(ev, GenerationDone):
            return "done_trimmed_by_proxy"
    return "no_terminal_event"


@pytest.mark.parametrize(
    ("repeat", "acceptable"),
    [
        pytest.param(
            _DEFAULT_REPEAT,
            frozenset(
                {
                    "failed_context_length",
                    "failed_invalid_request",
                    "done_trimmed_by_proxy",
                }
            ),
            id="context_window_exceeded",
        ),
        pytest.param(
            _OVERSIZED_REPEAT,
            frozenset(
                {
                    "failed_413_payload_too_large",
                    "failed_invalid_request",
                    "failed_connection",
                    "failed_timeout",
                }
            ),
            id="payload_too_large",
        ),
    ],
)
def test_context_overflow(repeat: int, acceptable: frozenset[str]) -> None:
    events = _run(repeat)
    outcome = _classify(events)
    print(f"[test] outcome={outcome}")  # noqa: T201
    assert outcome in acceptable, (
        f"unexpected outcome={outcome}; "
        f"acceptable={sorted(acceptable)}; "
        f"events={[type(e).__name__ for e in events]}"
    )


if __name__ == "__main__":
    # Ручной запуск: python test_context_overflow.py [repeat]
    cli_repeat = int(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_REPEAT
    cli_outcome = _classify(_run(cli_repeat))
    print(f"[test] outcome={cli_outcome}")  # noqa: T201
