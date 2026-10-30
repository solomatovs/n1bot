"""Точка входа: env chainlit выставляется до первого импорта его модулей.

Ошибки:
ConfigError — конфиг не загружается, секции [app].aid, [app].session,
    [instance] не сходятся с моделями либо каталог программы не найден.
SystemExit — проба готовности: конфиг не прочитан или чат не ответил.
"""

import argparse
import os
import pathlib
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from omegaconf import DictConfig

from boba.aid.infra.config import AidSection
from boba.config import ConfigError, ConfigLoader, bind
from boba.runtime.config import (
    ConfigArguments,
    InstanceConfig,
    InstanceData,
    ProgramCall,
    ProgramCommand,
    ProgramDir,
    ProgramRoots,
    RawConfig,
    SessionConfig,
)
from boba.runtime.http import HealthCheckError, HealthProbe

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
    приложения (share/ каталога программы, только для чтения), и писать в него
    нельзя. Поэтому первый импорт chainlit.config идёт здесь: создание
    <APP_ROOT>/.files пропускается, а FILES_DIRECTORY подменяется каталогом
    files/ данных экземпляра раньше, чем его прочтут сессии и chainlit.server.
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
    run(): `serve` поднимает чат, `health` пробует готовность запущенного.
    Конфиг загружается один раз, и то же дерево получают env chainlit, каталог
    вложений и сборка приложения. Статика chainlit — share/ каталога
    программы (ProgramRoots), вложения — files/ каталога данных экземпляра.
    Тесты точки входа зовут export_env и attachments по отдельности.
    """

    SECTION: ClassVar[str] = "app.aid"

    SESSION_SECTION: ClassVar[str] = "app.session"

    HEALTH: ClassVar[str] = "/"
    """Путь пробы готовности под префиксом чата: страница входа chainlit."""

    def __init__(self) -> None:
        self._programs = ProgramRoots()

    def run(self) -> None:
        call = self.call()
        match call.command:
            case ProgramCommand.SERVE:
                self.serve(call.paths)
            case ProgramCommand.HEALTH:
                self.health(call.paths)

    def call(self) -> ProgramCall:
        """Подкоманда и файлы конфига: конфиг экземпляра и наложения."""
        parser = argparse.ArgumentParser(
            prog="boba-aid",
            description="Aid: the chat web application of boba",
        )

        return ConfigArguments(parser).call()

    def serve(self, paths: Sequence[Path]) -> None:
        raw = RawConfig.load(paths)
        self.export_env(raw)
        self.attachments(raw).install()

        # импорт здесь: chainlit фиксирует пути из env на импорте своих модулей
        from boba.aid.infra.bootstrap import run_app  # noqa: PLC0415

        run_app(raw, paths[0].resolve().parent)

    def health(self, paths: Sequence[Path]) -> None:
        """Проба готовности по [app].aid: порт и префикс чата.

        Ошибки:
        SystemExit — конфиг не прочитан или чат не ответил успехом.
        """
        try:
            aid = bind(ConfigLoader(paths).load(), self.SECTION, AidSection)
            checked = HealthProbe(aid.port, aid.url_prefix, self.HEALTH).check()
        except (ConfigError, HealthCheckError) as exc:
            raise SystemExit(f"boba-aid health: {exc}") from exc

        sys.stdout.write(f"{checked}\n")

    def attachments(self, raw: DictConfig) -> ChainlitFiles:
        """Каталог вложений — files/ данных экземпляра."""
        instance = bind(raw, InstanceConfig.SECTION, InstanceConfig)
        root = Path(os.environ[ChainlitEnv.APP_ROOT])

        return ChainlitFiles(root, instance.data(InstanceData.FILES))

    def export_env(self, raw: DictConfig) -> None:
        """share/ программы, секции [app].aid и [app].session -> переменные
        окружения chainlit."""
        aid = bind(raw, self.SECTION, AidSection)
        session = bind(raw, self.SESSION_SECTION, SessionConfig)
        share = self._programs.locate().path(ProgramDir.SHARE)

        os.environ[ChainlitEnv.APP_ROOT] = str(share)
        os.environ[ChainlitEnv.ROOT_PATH] = aid.url_prefix

        os.environ[ChainlitEnv.AUTH_SECRET] = session.auth_secret
        os.environ[ChainlitEnv.COOKIE_NAME] = session.cookie
        os.environ[ChainlitEnv.COOKIE_SAMESITE] = session.cookie_samesite


def cli() -> None:
    """Консольная команда boba-aid."""
    AppEntry().run()
