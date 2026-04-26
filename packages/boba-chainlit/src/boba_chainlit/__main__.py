"""Bootstrap chainlit-приложения.

Собирает application :class:`ConfigBundle` из env+TOML, читает из него
chainlit-секции (:class:`ChainlitSection`, :class:`ChainlitUiOverrideSection`)
и:

1. прокидывает server-параметры в ``CHAINLIT_*`` env (chainlit-библиотека
   читает их при импорте);
2. рендерит ``.chainlit/config.toml`` для UI-оверрайдов (chainlit
   смотрит TOML только при старте сервера);
3. импортирует chainlit и запускает.
"""

from __future__ import annotations

import os
from pathlib import Path

from boba.domain.core.config import ChainedConfigResolver
from boba.infra import (
    AgentSection,
    AppCoreSection,
    ConfigBundle,
    ConfigFactory,
    ConfigLoader,
)
from boba_adapter_fs_workspace import WorkspacesSection
from boba_adapter_openai import LLMTransportSection
from boba_adapter_prompt_providers import PromptsSection
from boba_chainlit.config import ChainlitConfig, ChainlitSection
from boba_chainlit.ui_overrides import (
    ChainlitUiOverrideSection,
    UIOverrideTomlConverter,
)
from boba_config_env import EnvFileSource, EnvSource
from boba_config_toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
    load_toml,
)


def _build_resolver() -> ChainedConfigResolver:
    """Локальный резолвер для bootstrap'а: env (с file-указателем) +
    TOML (с file-указателем). Тот же набор, что у ChatSession.
    """
    toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
    return ChainedConfigResolver(
        [
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(toml_data),
            TomlSource(toml_data),
        ]
    )


def _bootstrap_env(cfg: ChainlitConfig) -> None:
    os.environ.setdefault("CHAINLIT_HOST", cfg.host)
    os.environ.setdefault("CHAINLIT_PORT", cfg.port)
    if cfg.root_path:
        os.environ.setdefault("CHAINLIT_ROOT_PATH", cfg.root_path)
    if cfg.auth_secret:
        os.environ.setdefault("CHAINLIT_AUTH_SECRET", cfg.auth_secret)
    os.environ.setdefault("CHAINLIT_HEADLESS", cfg.headless)


def _write_ui_config_overrides(bundle: ConfigBundle) -> None:
    """Рендерит ``.chainlit/config.toml`` из UI-оверрайдов до импорта chainlit.

    Логика разнесена по слоям: чтение из бандла → :class:`UIOverride`,
    сериализация :class:`UIOverride` → TOML-строка. Здесь остался только
    оркестратор: «прочитай, отрендерь, если не пусто — запиши».
    """
    override = bundle.section(ChainlitUiOverrideSection)
    content = UIOverrideTomlConverter().convert(override)
    if not content:
        return
    target = Path.cwd() / ".chainlit" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _build_factory() -> ConfigFactory:
    """Регистрирует встроенные секции (``app_core``/``agent``) и
    adapter-секции (FS-workspace, OpenAI-транспорт, file-prompt loader).
    Расширения через entry-point group ``boba.config_sections``
    подхватываются после.
    """
    factory = ConfigFactory(_build_resolver())
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(PromptsSection())
    factory.discover_extension_sections()
    return factory


def main() -> None:
    loader = ConfigLoader(_build_factory())
    bundle = loader.load_bundle()
    _bootstrap_env(bundle.section(ChainlitSection))
    _write_ui_config_overrides(bundle)

    # Импорт chainlit — только после bootstrap: модуль читает env при загрузке.
    from chainlit.cli import run_chainlit  # noqa: PLC0415

    app_path = Path(__file__).with_name("app.py")
    run_chainlit(str(app_path))


if __name__ == "__main__":
    main()
