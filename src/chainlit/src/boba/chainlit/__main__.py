"""Точка входа: ``python -m boba.chainlit``.

Загружает секцию ``[chainlit]`` из ``BOBA_CONFIG`` и прокидывает её как
env-переменные (``CHAINLIT_HOST``, ``CHAINLIT_PORT``, ``CHAINLIT_ROOT_PATH``,
``CHAINLIT_AUTH_SECRET``) **до** импорта ``chainlit`` — фреймворк читает
их один раз при инициализации. Затем вызывает ``chainlit.cli.run_chainlit``
с путём к :mod:`boba.chainlit.app`.

Секрет аутентификации допускается задать файлом (Docker secret) через
``CHAINLIT_AUTH_SECRET_FILE`` или поле ``auth_secret_file`` в
``[chainlit]`` — помимо обычной env-переменной ``CHAINLIT_AUTH_SECRET``.
Приоритет: прямая env → file → TOML-value → default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from boba.chainlit.config import load_chainlit_section


def _resolve(
    env_key: str,
    toml_section: dict[str, Any],
    toml_key: str,
    default: str | None = None,
) -> str | None:
    val = os.environ.get(env_key)
    if val is not None:
        return val
    file_env = os.environ.get(f"{env_key}_FILE")
    if file_env:
        p = Path(file_env)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    file_toml = toml_section.get(f"{toml_key}_file")
    if isinstance(file_toml, str):
        p = Path(file_toml)
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    toml_val = toml_section.get(toml_key)
    if toml_val is not None:
        return str(toml_val)
    return default


def _bootstrap_env() -> None:
    section = load_chainlit_section()
    host = _resolve("CHAINLIT_HOST", section, "host", "0.0.0.0")
    port = _resolve("CHAINLIT_PORT", section, "port", "8000")
    root_path = _resolve("CHAINLIT_ROOT_PATH", section, "root_path", "")
    auth_secret = _resolve("CHAINLIT_AUTH_SECRET", section, "auth_secret")
    headless = _resolve("CHAINLIT_HEADLESS", section, "headless", "true")

    # ``setdefault`` — внешний env всегда выигрывает, это позволяет
    # оверрайдить из docker-compose без правки TOML.
    if host is not None:
        os.environ.setdefault("CHAINLIT_HOST", host)
    if port is not None:
        os.environ.setdefault("CHAINLIT_PORT", port)
    if root_path:
        os.environ.setdefault("CHAINLIT_ROOT_PATH", root_path)
    if auth_secret:
        os.environ.setdefault("CHAINLIT_AUTH_SECRET", auth_secret)
    if headless is not None:
        os.environ.setdefault("CHAINLIT_HEADLESS", headless)


def main() -> None:
    _bootstrap_env()

    # Импорт chainlit — только после bootstrap: модуль читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))


if __name__ == "__main__":
    main()
