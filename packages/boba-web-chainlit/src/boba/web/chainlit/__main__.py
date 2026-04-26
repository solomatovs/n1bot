"""Bootstrap chainlit-приложения.

Здесь — единственное место в пакете, где собирается ConfigBundle и
строится цепочка ConfigSource'ов. :class:`ChatSession` получает уже
готовый bundle через ``ChatSession.from_bundle(bundle)`` — никакого
повторного похода в env/TOML.

Шаги:

1. собрать ConfigBundle (env + TOML, плюс расширения через
   entry-points);
2. через :func:`bridge_chainlit_env` прокинуть chainlit-server-поля
   в ``CHAINLIT_*`` env (chainlit-библиотека читает их при импорте) и
   получить абсолютный ``app_root``;
3. через :class:`UIOverrideTomlConverter` отрендерить
   ``app_root/.chainlit/config.toml`` для UI-overrides (chainlit для
   этих полей env не смотрит, только TOML);
4. импортировать chainlit и запустить ``run_chainlit(app.py)``.
"""

from __future__ import annotations

import os
from pathlib import Path

from boba.adapter.fs_workspace import WorkspacesSection
from boba.adapter.openai import LLMTransportSection
from boba.adapter.prompt_providers import PromptsSection
from boba.config.env import EnvFileSource, EnvSource
from boba.config.toml import (
    CONFIG_PATH_ENV,
    TomlFileSource,
    TomlSource,
    load_toml,
)
from boba.domain.core.config import ChainedConfigResolver
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


def build_bundle() -> ConfigBundle:
    """Собирает application :class:`ConfigBundle`.

    Цепочка источников: env-file > env > toml-file > toml. Регистрирует
    встроенные секции (``app_core``/``agent``) и adapter-секции (FS-
    workspace, OpenAI-транспорт, file-prompt loader, chainlit).
    Расширения через entry-point group ``boba.config_sections``
    подхватываются после.
    """
    toml_data = load_toml(os.environ.get(CONFIG_PATH_ENV))
    resolver = ChainedConfigResolver(
        [
            EnvFileSource(),
            EnvSource(),
            TomlFileSource(toml_data),
            TomlSource(toml_data),
        ]
    )
    factory = ConfigFactory(resolver)
    factory.register(AppCoreSection())
    factory.register(AgentSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(PromptsSection())
    factory.register(ChainlitSection())
    factory.discover_extension_sections()
    return ConfigLoader(factory).load_bundle()


def bridge_chainlit_env(cfg: ChainlitConfig) -> Path:
    """Прокидывает поля :class:`ChainlitConfig` в ``CHAINLIT_*`` env, что
    chainlit-библиотека читает при импорте. Возвращает абсолютный
    ``app_root`` — он же используется для записи UI-overrides.
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
    """Рендерит ``app_root/.chainlit/config.toml`` из UI-полей конфига.

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


def main() -> None:
    bundle = build_bundle()
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


if __name__ == "__main__":
    main()
