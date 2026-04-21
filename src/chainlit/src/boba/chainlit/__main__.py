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


def _write_ui_config_overrides() -> None:
    """Рендерит `.chainlit/config.toml` с оверрайдами из env до импорта chainlit.

    Сам chainlit читает UI/features/project-настройки только из TOML —
    env-переменные для этих секций не поддерживает. Мост:

    - CHAINLIT_UI_NAME               → [UI] name
    - CHAINLIT_ENABLE_TELEMETRY      → [project] enable_telemetry  (true/false)
    - CHAINLIT_FILE_UPLOAD_MAX_MB    → [features.spontaneous_file_upload] max_size_mb
    - CHAINLIT_FILE_UPLOAD_MAX_FILES → [features.spontaneous_file_upload] max_files
    - CHAINLIT_FILE_UPLOAD_ACCEPT    → [features.spontaneous_file_upload] accept (CSV)

    Пишем только если хоть одна env задана — иначе chainlit сгенерит
    свои дефолты. Если задана хоть одна из CHAINLIT_FILE_UPLOAD_* —
    автоматически выставляем `enabled = true` у secondary section,
    иначе chainlit скрывает кнопку загрузки.
    """
    name = os.environ.get("CHAINLIT_UI_NAME")
    telemetry = os.environ.get("CHAINLIT_ENABLE_TELEMETRY")
    up_max_mb = os.environ.get("CHAINLIT_FILE_UPLOAD_MAX_MB")
    up_max_files = os.environ.get("CHAINLIT_FILE_UPLOAD_MAX_FILES")
    up_accept = os.environ.get("CHAINLIT_FILE_UPLOAD_ACCEPT")

    overrides: list[str] = []

    if telemetry is not None:
        flag = "true" if telemetry.strip().lower() in ("true", "1", "yes", "on") else "false"
        overrides += ["[project]", f"enable_telemetry = {flag}", ""]

    has_upload = any(v is not None for v in (up_max_mb, up_max_files, up_accept))
    if has_upload:
        accept_list = [s.strip() for s in (up_accept or "*/*").split(",") if s.strip()]
        accept_toml = "[" + ", ".join(f'"{a}"' for a in accept_list) + "]"
        overrides += ["[features.spontaneous_file_upload]", "enabled = true"]
        if up_max_mb is not None:
            overrides.append(f"max_size_mb = {int(up_max_mb)}")
        if up_max_files is not None:
            overrides.append(f"max_files = {int(up_max_files)}")
        overrides.append(f"accept = {accept_toml}")
        overrides.append("")

    if name is not None:
        overrides += ["[UI]", f'name = "{name}"', ""]

    if not overrides:
        return

    # `[meta] generated_by` обязателен: chainlit проверяет версию на старте
    # и падает если не задан (или <= "0.3.0"). Строка "boba-chainlit" >
    # "0.3.0" в лексикографическом сравнении — проходит валидацию.
    overrides += ["[meta]", 'generated_by = "boba-chainlit"', ""]

    target = Path.cwd() / ".chainlit" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(overrides), encoding="utf-8")


def main() -> None:
    _bootstrap_env()
    _write_ui_config_overrides()

    # Импорт chainlit — только после bootstrap: модуль читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))


if __name__ == "__main__":
    main()
