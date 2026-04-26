"""Общие фикстуры/bootstrap для тестов.

Подставляет пути к артефактам внутри ``.vscode/`` (workspaces, config.toml,
secrets), чтобы ``pytest`` из корня репозитория не засорял root и сразу
находил конфиг без ручного экспорта env-переменных. Значения ставятся
через :meth:`os.environ.setdefault` — VS Code launch.json и CI, где
переменные уже заданы, имеют приоритет.
"""

from __future__ import annotations

import os
from pathlib import Path


def _bootstrap_test_env() -> None:
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
