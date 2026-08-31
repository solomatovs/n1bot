"""Точка входа: env chainlit выставляется до первого импорта его модулей."""

import argparse
import os
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from omegaconf import OmegaConf

from boba.chainlit.domain.keys import AppPrefix
from boba.config import bind
from boba.runtime.config import AppLayers, SessionConfig

__all__ = ["AppEntry", "ChainlitEnv"]


class ChainlitEnv(StrEnum):
    """Переменные окружения, которые chainlit читает на импорте своих модулей."""

    APP_ROOT = "CHAINLIT_APP_ROOT"
    AUTH_SECRET = "CHAINLIT_AUTH_SECRET"  # noqa: S105 — имя переменной, не секрет
    COOKIE_NAME = "CHAINLIT_AUTH_COOKIE_NAME"
    COOKIE_SAMESITE = "CHAINLIT_COOKIE_SAMESITE"


class AppEntry:
    """Конфиг -> env chainlit -> запуск приложения."""

    SECTION: ClassVar[str] = "app.chainlit"

    SESSION_SECTION: ClassVar[str] = "session"

    @classmethod
    def run(cls) -> None:
        config_path = cls.config_argument()
        cls.export_env(config_path)

        # импорт здесь: chainlit фиксирует пути из env на импорте своих модулей
        from boba.chainlit.infra.bootstrap import run_app  # noqa: PLC0415

        run_app(config_path)

    @classmethod
    def config_argument(cls) -> Path:
        """Путь конфига — обязательный аргумент запуска; дефолта и env нет."""
        parser = argparse.ArgumentParser(
            prog="boba.chainlit",
            description="Chainlit application of boba",
        )
        parser.add_argument(
            "--config",
            required=True,
            type=Path,
            help="path to the application config.toml",
        )
        arguments = parser.parse_args()

        return arguments.config

    @classmethod
    def export_env(cls, config_path: Path) -> None:
        """Секции [chainlit] и [session] -> переменные окружения chainlit."""
        raw = AppLayers.compose(config_path)
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
