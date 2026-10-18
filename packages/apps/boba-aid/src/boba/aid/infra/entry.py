"""Точка входа: env chainlit выставляется до первого импорта его модулей.

Ошибки:
ValueError — в конфиге нет секции [aid] либо пусты root или files_dir.
"""

import argparse
import os
import pathlib
from collections.abc import Generator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from omegaconf import DictConfig, OmegaConf

from boba.aid.infra.config import AidSection
from boba.config import bind
from boba.runtime.config import (
    AppLayers,
    ConfigArguments,
    ConfigFiles,
    SessionConfig,
)

__all__ = ["AppEntry", "ChainlitEnv", "ChainlitFiles", "cli"]


class ChainlitEnv(StrEnum):
    """Переменные окружения, которые chainlit читает на импорте своих модулей."""

    APP_ROOT = "CHAINLIT_APP_ROOT"
    AUTH_SECRET = "CHAINLIT_AUTH_SECRET"  # noqa: S105 — имя переменной, не секрет
    COOKIE_NAME = "CHAINLIT_AUTH_COOKIE_NAME"
    COOKIE_SAMESITE = "CHAINLIT_COOKIE_SAMESITE"
    ROOT_PATH = "CHAINLIT_ROOT_PATH"


class ChainlitFiles:
    """Каталог вложений chainlit вне app_root.

    chainlit держит вложения сессий в <APP_ROOT>/.files: путь собирает и каталог
    создаёт на импорте chainlit.config, настройки для него нет. app_root — статика
    приложения (в отладке — ассеты пакета, в образе — слой только для чтения), и
    писать в него нельзя. Поэтому первый импорт chainlit.config идёт здесь: создание
    <APP_ROOT>/.files пропускается, а FILES_DIRECTORY подменяется каталогом из
    [aid].files_dir раньше, чем его прочтут сессии и chainlit.server.
    """

    NAME: ClassVar[str] = ".files"

    def __init__(self, app_root: Path, files_dir: Path) -> None:
        self._inside_root = app_root / self.NAME
        self._files_dir = files_dir

    def install(self) -> None:
        self._files_dir.mkdir(parents=True, exist_ok=True)

        with self._without_root_files():
            import chainlit.config  # noqa: PLC0415 — пути chainlit фиксирует на импорте

        chainlit.config.FILES_DIRECTORY = self._files_dir

    @contextmanager
    def _without_root_files(self) -> Generator[None, None, None]:
        """На время импорта Path.mkdir пропускает <APP_ROOT>/.files."""
        skipped = self._inside_root
        mkdir = pathlib.Path.mkdir

        def guarded(
            path: Path,
            mode: int = 0o777,
            parents: bool = False,
            exist_ok: bool = False,
        ) -> None:
            if path == skipped:
                return

            mkdir(path, mode, parents, exist_ok)

        # setattr: подмена метода класса на время импорта, присваивание атрибуту
        # типизация справедливо не пропускает
        setattr(pathlib.Path, "mkdir", guarded)  # noqa: B010
        try:
            yield
        finally:
            setattr(pathlib.Path, "mkdir", mkdir)  # noqa: B010


class AppEntry:
    """Конфиг -> env chainlit -> каталог вложений -> запуск приложения.

    Создаёт его скрипт чата (main.py, консольная команда boba-aid) и зовёт
    run(); тесты точки входа зовут export_env и attachments по отдельности.
    """

    SECTION: ClassVar[str] = "app.aid"

    SESSION_SECTION: ClassVar[str] = "session"

    def run(self) -> None:
        files = self.config_files()
        self.export_env(files)
        self.attachments(files).install()

        # импорт здесь: chainlit фиксирует пути из env на импорте своих модулей
        from boba.aid.infra.bootstrap import run_app  # noqa: PLC0415

        run_app(files)

    def config_files(self) -> ConfigFiles:
        """Пути общего конфига и site-файла — обязательные аргументы запуска."""
        parser = argparse.ArgumentParser(
            prog="boba.aid",
            description="Aid: the chat web application of boba",
        )

        return ConfigArguments(parser).files()

    def attachments(self, files: ConfigFiles) -> ChainlitFiles:
        """Каталог вложений из [aid].files_dir; пустое значение отвергается."""
        aid = self._section(files, AppLayers.compose(files))
        if not aid.files_dir:
            msg = (
                f"{files.config}: section [{self.SECTION}] expects files_dir as a "
                f"non-empty path (chainlit attachments outside app root), "
                f"got {aid.files_dir!r}"
            )
            raise ValueError(msg)

        root = Path(os.environ[ChainlitEnv.APP_ROOT])

        return ChainlitFiles(root, Path(aid.files_dir).resolve())

    def export_env(self, files: ConfigFiles) -> None:
        """Секции [aid] и [session] -> переменные окружения chainlit."""
        raw = AppLayers.compose(files)
        aid = self._section(files, raw)
        if not aid.root:
            msg = (
                f"{files.config}: section [{self.SECTION}] expects root as a "
                f"non-empty path (chainlit app root), got {aid.root!r}"
            )
            raise ValueError(msg)

        session = bind(raw, self.SESSION_SECTION, SessionConfig)

        # chainlit складывает пути от APP_ROOT сам, относительный сбился бы на chdir
        os.environ[ChainlitEnv.APP_ROOT] = str(Path(aid.root).resolve())
        os.environ[ChainlitEnv.ROOT_PATH] = aid.url_prefix

        os.environ[ChainlitEnv.AUTH_SECRET] = session.auth_secret
        os.environ[ChainlitEnv.COOKIE_NAME] = session.cookie
        os.environ[ChainlitEnv.COOKIE_SAMESITE] = session.cookie_samesite

    def _section(self, files: ConfigFiles, raw: DictConfig) -> AidSection:
        """Секция [aid] моделью; без секции — ValueError с путём конфига."""
        if OmegaConf.select(raw, self.SECTION) is None:
            msg = f"{files.config}: section [{self.SECTION}] is missing"
            raise ValueError(msg)

        return bind(raw, self.SECTION, AidSection)


def cli() -> None:
    """Консольная команда boba-aid."""
    AppEntry().run()
