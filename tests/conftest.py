"""Общие фикстуры для интеграционных тестов агента.

Bootstrap окружения для тестов: подставляет пути к артефактам внутри
``.vscode/`` (workspaces, config.toml, secrets, plugins), чтобы
``pytest`` из корня репозитория не засорял root-директорию и сразу
находил конфиг без ручного экспорта env-переменных. Значения ставятся
через :meth:`os.environ.setdefault` — VS Code launch.json или
CI-окружение всегда имеют приоритет.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _bootstrap_test_env() -> None:
    """Перенаправить артефакты тестов в ``.vscode/`` и указать plugins-папку.

    Всё ставится через :meth:`os.environ.setdefault` — VS Code launch.json
    и CI, где переменные уже заданы, имеют приоритет.
    """
    repo_root = Path(__file__).resolve().parent.parent
    vscode_dir = repo_root / ".vscode"

    defaults: dict[str, str] = {
        "WORKSPACE_BASE_DIR": str(vscode_dir / "workspaces"),
        "LOG_FILE": str(vscode_dir / "logs" / "tests.log"),
        # Дефолтная директория промптов для тестов. Конфиг считает
        # поле обязательным. Tool-плагины подгружаются через
        # entry-points установленных pip-пакетов (см. README по
        # установке расширений).
        "BOBA_PROMPTS_DIR": str(repo_root / "prompts"),
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
    :mod:`tests.boba.test_agent_loop`; legacy-тесты рядом тоже
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
