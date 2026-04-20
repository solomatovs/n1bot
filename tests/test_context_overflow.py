"""Интеграционный тест: проверка поведения при переполнении контекста.

Отправляет user-prompt заведомо больше context window выбранной модели.
Возможные исходы (каждый полезен диагностически):

1. **Желаемый — HTTP 400 / ``context_length_exceeded``.**
   Адаптер маппит в :class:`LLMContextLengthError` (``PermanentLLMError``),
   :class:`ConsoleSink` печатает красное
   ``[llm error: LLMContextLengthError (permanent) [status=400]] ...``.
   Retry не сработает — `PermanentLLMError` не-``Retryable``,
   цикл остановит :class:`StopOnAnyFailure`.

2. **Прокси молча обрезает контекст** (типично для LiteLLM
   с ``context_window_fallback`` / ``max_input_tokens``).
   Запрос примут, модель ответит по урезанному prompt'у. В логах
   ``LLM done: … finish=stop/length, max_tokens=…`` как обычно.
   Это НЕ проблема нашего кода — это настройка прокси. Чтобы
   увидеть ошибку: отключить auto-trim на LiteLLM ИЛИ увеличить
   ``--repeat`` до значения, превышающего физический лимит (HTTP
   тело/proxy buffer).

3. **HTTP 413 Payload Too Large** (если прокси за nginx/envoy с
   ``client_max_body_size``). Мапится в
   :class:`LLMInvalidRequestError` (status=413).

Запуск: VS Code launch ``Test Context Overflow`` (см. ``launch.json``).
Первый CLI-аргумент — число повторов фразы (по умолчанию 100_000 →
~1.2 MB / 300–500K токенов русского, гарантированно больше любого
стандартного окна qwen3).
"""

from __future__ import annotations

import sys
from dataclasses import replace

from boba.app.logging import configure_logging
from boba.domain.agent.meat import Agent
from boba.domain.agent.models import AgentRequest, RequestId
from boba.domain.core.workspace import WorkspaceManager
from boba.infra.config import ConfigLoader
from boba.infra.container import create_container, request_scope

_DEFAULT_REPEAT = 100_000  # ~1.2 MB → 300–500K токенов русского
_PHRASE = "ААА БББ ВВВ "


def _build_huge_query(repeat: int) -> str:
    return _PHRASE * repeat + "теперь ответь одним словом: ок"


def test_context_overflow() -> None:
    repeat = int(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_REPEAT
    query = _build_huge_query(repeat)
    # Cyrillic BPE для современных токенизаторов: ≈ 1 символ ≈ 1 токен
    # (часто больше на юникод-парах). Оценка грубая — реально больше.
    print(  # noqa: T201
        f"[test] repeat={repeat}, query length: {len(query)} chars "
        f"(грубая оценка ≈ {len(query)}–{len(query) * 2} токенов cyrillic)"
    )

    loader = ConfigLoader()
    app_config = loader.load_app()
    configure_logging(app_config.log_level)
    # один turn достаточно: либо 400, либо нормальный ответ;
    # 1.2 MB тело на каждой итерации не гоняем зря.
    agent_config = replace(loader.load_agent(), max_iterations=1)
    llm_defaults = loader.load_llm_defaults()
    container = create_container(app_config, agent_config, llm_defaults)

    manager = container.get(WorkspaceManager)
    storage = manager.create()
    print(f"[test] workspace: {storage.workspace_id}")  # noqa: T201

    with request_scope(container, storage.workspace_id) as req:
        agent = req.get(Agent)
        request = AgentRequest(
            query=query,
            model=app_config.llm.model,
            workspace_id=storage.workspace_id,
            request_id=RequestId.new(),
        )
        agent.run(agent_config, request)


if __name__ == "__main__":
    test_context_overflow()
