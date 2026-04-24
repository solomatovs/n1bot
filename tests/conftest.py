"""Общие фикстуры для интеграционных тестов агента.

Всё «инфраструктурное» (конфиг, логи, контейнер, scope, сбор событий)
живёт в :class:`boba.infra.AgentHarness`. Тестам остаётся запросить
фикстуру и вызвать ``harness.ask(ws_id, query)``.

Этот модуль также выполняет bootstrap окружения для тестов: подставляет
пути к артефактам внутри ``.vscode/`` (workspaces, config.toml,
secrets), чтобы ``pytest`` из корня репозитория не засорял root-директорию
и сразу находил конфиг без ручного экспорта env-переменных. Значения
ставятся через :meth:`os.environ.setdefault` — VS Code launch.json или
CI-окружение всегда имеют приоритет.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from boba.domain.agent.events import AgentEvent, GenerationDone, GenerationFailed
from boba.infra import AgentHarness


def _bootstrap_test_env() -> None:
    """Перенаправить артефакты тестов в ``.vscode/``.

    Всё ставится через :meth:`os.environ.setdefault` — VS Code launch.json
    и CI, где переменные уже заданы, имеют приоритет. `AgentHarness` читает
    env только в момент построения (внутри фикстуры), так что bootstrap
    успевает отработать раньше.
    """
    repo_root = Path(__file__).resolve().parent.parent
    vscode_dir = repo_root / ".vscode"

    defaults: dict[str, str] = {
        "WORKSPACE_BASE_DIR": str(vscode_dir / "workspaces"),
        "LOG_FILE": str(vscode_dir / "logs" / "tests.log"),
    }
    config_toml = vscode_dir / "config" / "config.toml"
    if config_toml.is_file():
        defaults["BOBA_CONFIG"] = str(config_toml)
    api_key = vscode_dir / "secrets" / "litellm_api_key"
    if api_key.is_file():
        defaults["LITELLM_API_KEY_FILE"] = str(api_key)

    for key, value in defaults.items():
        os.environ.setdefault(key, value)


_bootstrap_test_env()

OutcomeClassifier = Callable[[Iterable[AgentEvent]], str]


@pytest.fixture
def harness() -> AgentHarness:
    """AgentHarness с ``max_iterations=1`` — для smoke-прогонов одного запроса."""
    return AgentHarness(max_iterations=1)


@pytest.fixture
def model() -> str:
    """Имя модели для интеграционных тестов — только из env ``LLM_MODEL``.

    Системного дефолта нет: чтобы тест гонял конкретную модель, её надо
    задать явно (в launch.json / CI / ручном запуске).
    """
    value = os.environ.get("LLM_MODEL")
    if not value:
        msg = (
            "LLM_MODEL env var is required for integration tests — "
            "set it explicitly, no system default"
        )
        raise RuntimeError(msg)
    return value


@pytest.fixture
def query() -> str:
    """Query для integration-тестов — только из env ``LLM_QUERY``.

    Системного дефолта нет (симметрично ``model``). Используется
    :mod:`tests.boba_2.test_agent_loop`; legacy-тесты рядом тоже
    объявляли ``query`` в сигнатуре — теперь fixture единая на весь
    репозиторий.
    """
    value = os.environ.get("LLM_QUERY")
    if not value:
        msg = (
            "LLM_QUERY env var is required for integration tests — "
            "set it explicitly, no system default"
        )
        raise RuntimeError(msg)
    return value


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
