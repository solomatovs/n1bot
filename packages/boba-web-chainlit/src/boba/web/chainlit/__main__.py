"""Bootstrap chainlit-приложения.

Единственное место в пакете, где собирается ConfigBundle. ChatSession
получает готовый bundle через set_bundle() — без повторного похода в env/TOML.

Шаги: ConfigBundle (CLI argparse строится из FieldSpec'ев на bind_schema,
env/TOML читают свои каналы сами) → bridge_chainlit_env (server-поля в
CHAINLIT_* env, app_root) → UIOverrideTomlConverter (UI-поля в
app_root/.chainlit/config.toml) → run_chainlit(app.py).

Имена CLI-флагов вычисляются из ConfigKey'ев секции (см. cli_flag_name);
``--help`` автогенерируется через CliSource по всем зарегистрированным
секциям, опечатки во флагах валятся argparse-ошибкой на bind_schema.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from boba.adapter.fs_workspace import WorkspacesSection
from boba.adapter.openai import LLMTransportSection
from boba.adapter.prompt_providers import PromptsSection
from boba.config.cli import CliSource
from boba.config.env import EnvFileSource, EnvSource
from boba.config.toml import CONFIG_PATH_ENV, TomlFileSource, TomlSource
from boba.domain.core.patterns import ConverterInputError
from boba.infra import (
    AgentSection,
    AppCoreSection,
    ConfigBundle,
    ConfigFactory,
    ConfigLoader,
)
from boba.web.chainlit.config import ChainlitConfig, ChainlitSection
from boba.web.chainlit.session import ChatSession
from boba.web.chainlit.ui_overrides import UIOverrideTomlConverter

# ──────────────────────────────────────────────────────────────────────
# Bundle assembly
# ──────────────────────────────────────────────────────────────────────


def build_factory(argv: Sequence[str] | None = None) -> ConfigFactory:
    """Готовая ConfigFactory с зарегистрированными секциями и
    подключёнными источниками. Bundle ещё не собран — вызови
    ``ConfigLoader(factory).load_bundle()`` или ``factory.build()``.

    Цепочка источников: CLI > env-file > env > toml-file > toml.
    Регистрирует встроенные секции (app_core/agent), adapter-секции
    (FS-workspace, OpenAI, prompt-providers, chainlit) и поднимает
    extension-секции через entry-point group boba.config_sections.
    argparse-парсер строит CliSource из FieldSpec'ов всех зарегистри-
    рованных секций на стадии ``factory.build()``.
    """
    factory = ConfigFactory()
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(PromptsSection())
    factory.register(ChainlitSection())
    factory.discover_extension_sections()
    factory.attach_sources(
        [
            CliSource(argv),
            EnvFileSource(),
            EnvSource(extra_known={CONFIG_PATH_ENV}),
            TomlFileSource(),
            TomlSource(),
        ]
    )
    return factory


def build_bundle(argv: Sequence[str] | None = None) -> ConfigBundle:
    """Тонкая обёртка над build_factory + load_bundle. Не ловит
    ConverterInputError — caller'ы (main / тесты) сами решают, что с
    ним делать."""
    return ConfigLoader(build_factory(argv)).load_bundle()


# ──────────────────────────────────────────────────────────────────────
# Chainlit env bridge + UI overrides
# ──────────────────────────────────────────────────────────────────────


def bridge_chainlit_env(cfg: ChainlitConfig) -> Path:
    """Прокидывает поля ChainlitConfig в CHAINLIT_* env, что
    chainlit-библиотека читает при импорте. Возвращает абсолютный
    app_root — он же используется для записи UI-overrides.
    """
    os.environ.setdefault("CHAINLIT_HOST", cfg.host)
    os.environ.setdefault("CHAINLIT_PORT", cfg.port)
    if cfg.root_path:
        os.environ.setdefault("CHAINLIT_ROOT_PATH", cfg.root_path)
    if cfg.auth_secret:
        os.environ.setdefault("CHAINLIT_AUTH_SECRET", cfg.auth_secret)
    os.environ.setdefault("CHAINLIT_HEADLESS", cfg.headless)
    app_root = Path(cfg.app_root).resolve()
    app_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CHAINLIT_APP_ROOT", str(app_root))
    return app_root


def write_ui_config_overrides(cfg: ChainlitConfig, app_root: Path) -> None:
    """Рендерит app_root/.chainlit/config.toml из UI-полей конфига.

    Chainlit смотрит TOML только при старте сервера, поэтому делается
    до импорта chainlit. Пустая строка от конвертера → файл не пишется
    (chainlit использует свои дефолты).
    """
    content = UIOverrideTomlConverter().convert(cfg)
    if not content:
        return
    target = app_root / ".chainlit" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    # argv пробрасывается в CliSource через build_factory. None →
    # CliSource берёт sys.argv[1:] сам (совместимо с stdlib-конвенцией
    # entry-point script / python -m).
    factory = build_factory(argv)
    try:
        bundle = ConfigLoader(factory).load_bundle()
    except ConverterInputError as e:
        print(f"error: {factory.format_config_error(e)}", file=sys.stderr)
        return 2
    chainlit_cfg = bundle.section(ChainlitSection)
    app_root = bridge_chainlit_env(chainlit_cfg)
    write_ui_config_overrides(chainlit_cfg, app_root)

    # ChatSession будет создан лениво из bundle при первом cl.on_chat_start;
    # сюда передаём bundle через app-level фабрику.
    ChatSession.set_bundle(bundle)

    # Импорт chainlit — только после bootstrap: модуль читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))
    return 0


if __name__ == "__main__":
    main()
