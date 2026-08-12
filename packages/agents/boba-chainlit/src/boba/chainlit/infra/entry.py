"""Точка входа: env chainlit выставляется до первого импорта его модулей."""

import os
from pathlib import Path
from typing import ClassVar

from omegaconf import OmegaConf

from boba.settings import build_app_config

__all__ = ["AppEntry"]


class AppEntry:
    """Конфиг -> env chainlit -> запуск приложения."""

    CONFIG_ENV: ClassVar[str] = "BOBA_CONFIG_PATH"

    BASE_ENV: ClassVar[str] = "BOBA_BASE"

    CONFIG_IN_BASE: ClassVar[str] = "conf/config.toml"

    SECTION: ClassVar[str] = "app.chainlit"

    APP_ROOT_ENV: ClassVar[str] = "CHAINLIT_APP_ROOT"

    ROOT_PATH_ENV: ClassVar[str] = "CHAINLIT_ROOT_PATH"

    AUTH_SECRET_ENV: ClassVar[str] = "CHAINLIT_AUTH_SECRET"  # noqa: S105

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
        """Секция [chainlit] -> переменные окружения, которые читает chainlit."""
        raw = build_app_config(config_path=config_path)
        section = OmegaConf.select(raw, cls.SECTION)
        if section is None:
            msg = f"в конфиге нет секции {cls.SECTION}: {config_path}"
            raise ValueError(msg)

        root = section.get("root")
        if not root:
            msg = f"{cls.SECTION}.root не задан: chainlit уедет в текущий каталог"
            raise ValueError(msg)

        # chainlit складывает пути от APP_ROOT сам, относительный сбился бы на chdir
        os.environ[cls.APP_ROOT_ENV] = str(Path(root).resolve())
        os.environ[cls.ROOT_PATH_ENV] = section.get("url_prefix") or ""

        auth_secret = section.get("auth_secret")
        if auth_secret:
            os.environ[cls.AUTH_SECRET_ENV] = auth_secret
