from __future__ import annotations

import os
from pathlib import Path

from boba_chainlit.config import (
    AUTH_SECRET,
    HEADLESS,
    HOST,
    PORT,
    ROOT_PATH,
    chainlit_resolver,
)
from boba_chainlit.ui_overrides import UIOverrideTomlConverter, read_ui_override


def _bootstrap_env() -> None:
    r = chainlit_resolver()
    host = HOST.read(r)
    port = PORT.read(r)
    root_path = ROOT_PATH.read(r)
    auth_secret = AUTH_SECRET.read_opt(r)
    headless = HEADLESS.read(r)

    os.environ.setdefault("CHAINLIT_HOST", host)
    os.environ.setdefault("CHAINLIT_PORT", port)
    if root_path:
        os.environ.setdefault("CHAINLIT_ROOT_PATH", root_path)
    if auth_secret:
        os.environ.setdefault("CHAINLIT_AUTH_SECRET", auth_secret)
    os.environ.setdefault("CHAINLIT_HEADLESS", headless)


def _write_ui_config_overrides() -> None:
    """Рендерит ``.chainlit/config.toml`` из env-оверрайдов до импорта chainlit.

    Логика разнесена по слоям: чтение env → :class:`UIOverride`,
    сериализация :class:`UIOverride` → TOML-строка. Здесь остался
    только оркестратор: «прочитай, отрендерь, если не пусто — запиши».
    """
    content = UIOverrideTomlConverter().convert(read_ui_override())
    if not content:
        return
    target = Path.cwd() / ".chainlit" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def main() -> None:
    _bootstrap_env()
    _write_ui_config_overrides()

    # Импорт chainlit — только после bootstrap: модуль читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))


if __name__ == "__main__":
    main()
