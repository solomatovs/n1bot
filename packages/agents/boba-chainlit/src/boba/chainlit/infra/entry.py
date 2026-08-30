"""Точка входа: env chainlit выставляется до первого импорта его модулей."""

import os
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from omegaconf import OmegaConf

from boba.chainlit.domain.keys import AppPrefix
from boba.config import bind, build_app_config
from boba.runtime.config import SessionConfig

__all__ = ["AppEntry", "ChainlitEnv"]


class ChainlitEnv(StrEnum):
    """Переменные окружения, которые chainlit читает на импорте своих модулей."""

    APP_ROOT = "CHAINLIT_APP_ROOT"
    AUTH_SECRET = "CHAINLIT_AUTH_SECRET"  # noqa: S105 — имя переменной, не секрет
    COOKIE_NAME = "CHAINLIT_AUTH_COOKIE_NAME"
    COOKIE_SAMESITE = "CHAINLIT_COOKIE_SAMESITE"


class AppEntry:
    """Конфиг -> env chainlit -> запуск приложения."""

    CONFIG_ENV: ClassVar[str] = "BOBA_CONFIG_PATH"

    BASE_ENV: ClassVar[str] = "BOBA_BASE"

    CONFIG_IN_BASE: ClassVar[str] = "conf/config.toml"

    SECTION: ClassVar[str] = "app.chainlit"

    SESSION_SECTION: ClassVar[str] = "session"

    @classmethod
    def run(cls) -> None:
        config_path = cls.config_path()
        cls.export_env(config_path)

        # импорт здесь: chainlit фиксирует пути из env на импорте своих модулей
        from boba.chainlit.infra.bootstrap import run_app  # noqa: PLC0415

        run_app(config_path)

    @classmethod
    def config_path(cls) -> Path:
        """Путь конфига: явный BOBA_CONFIG_PATH либо conf/config.toml в BOBA_BASE."""
        if config_path := os.environ.get(cls.CONFIG_ENV):
            return Path(config_path)

        base = os.environ.get(cls.BASE_ENV)
        if not base:
            msg = f"не задан ни {cls.CONFIG_ENV}, ни {cls.BASE_ENV}"
            raise ValueError(msg)

        return Path(base) / cls.CONFIG_IN_BASE

    @classmethod
    def export_env(cls, config_path: Path) -> None:
        """Секции [chainlit] и [session] -> переменные окружения chainlit."""
        raw = build_app_config(config_path=config_path)
        section = OmegaConf.select(raw, cls.SECTION)
        if section is None:
            msg = f"в конфиге нет секции {cls.SECTION}: {config_path}"
            raise ValueError(msg)

        root = section.get("root")
        if not root:
            msg = f"{cls.SECTION}.root не задан: chainlit уедет в текущий каталог"
            raise ValueError(msg)

        session = bind(raw, cls.SESSION_SECTION, SessionConfig)

        # chainlit складывает пути от APP_ROOT сам, относительный сбился бы на chdir
        os.environ[ChainlitEnv.APP_ROOT] = str(Path(root).resolve())
        os.environ[AppPrefix.ENV] = section.get("url_prefix") or ""

        os.environ[ChainlitEnv.AUTH_SECRET] = session.auth_secret
        os.environ[ChainlitEnv.COOKIE_NAME] = session.cookie
        os.environ[ChainlitEnv.COOKIE_SAMESITE] = session.cookie_samesite
