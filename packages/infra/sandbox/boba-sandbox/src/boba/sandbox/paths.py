"""Поиск ресурсов программы и команд запуска внешнего слоя песочницы.

Ошибки:
ConfigError — каталог ресурсов исходной или установленной программы недоступен."""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from boba.config import ConfigError
from boba.sandbox import WorkspaceSpec
from boba.sandbox.commands import SandboxCommand
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import LauncherChain, LauncherEnv, LauncherInterpreter


class ProgramDir(StrEnum):
    """Части каталога программы: их программа находит сама, ключей-путей в
    конфиге экземпляра для них нет."""

    LIBEXEC = "libexec"
    """Вспомогательные программы: bwrap, fuse2fs, mke2fs (mcp)."""
    SHARE = "share"
    """Статика: корень приложения chainlit (aid)."""
    SANDBOX = "sandbox"
    """Шаблон workspace.ext4.gz (mcp)."""


class ProgramFile(StrEnum):
    """Файлы каталога программы относительно его корня."""

    WORKSPACE = "sandbox/workspace.ext4.gz"


class SourceTree(StrEnum):
    """Дерево исходников вокруг runtime/: по нему программа из .venv берёт то,
    что лежит в git, прямо из исходников."""

    PACKAGES = "packages"
    """Каталог пакетов: его наличие рядом с runtime/ и есть дерево исходников."""


@dataclass(frozen=True)
class ProgramRoot(ABC):
    """Каталог программы: только чтение, обновление — замена каталога.

    Базовый класс; наследники — установленная программа (InstalledProgram) и
    программа из исходников (SourceProgram), выбирает их ProgramRoots на
    границе конфига. Пользуются им способ запуска плагинов в песочнице (пути
    из конфигов плагинов, bwrap), хранилище образов workspace и вход чата
    (статика chainlit). root — каталог программы (у исходников — runtime/),
    interpreter — чем цепочка лаунчера песочницы запускает ту же программу во
    внешнем слое.
    """

    root: Path
    interpreter: LauncherInterpreter

    SYSTEM_BINARIES: ClassVar[tuple[str, ...]] = (
        "/usr/bin",
        "/usr/sbin",
        "/bin",
        "/sbin",
    )
    """Системные каталоги после libexec: из них берутся fusermount."""

    WORKSPACE_TARGET: ClassVar[str] = "/workspace"

    @abstractmethod
    def path(self, part: ProgramDir) -> Path:
        """Каталог части программы."""

    def located(self, path: str) -> Path:
        """Путь из конфига: абсолютный — как есть, относительный — от
        каталога программы. Так один конфиг плагина ведёт в образе в
        /app/boba, а из исходников — в runtime/."""
        given = Path(path)
        if given.is_absolute():
            return given

        return self.root / given

    def workspace_template(self) -> Path:
        return self.root / ProgramFile.WORKSPACE.value

    def binaries(self) -> TrustedBinaries:
        """Доверенные каталоги bwrap и fuse2fs: libexec программы, затем
        системные."""
        dirs = (str(self.path(ProgramDir.LIBEXEC)), *self.SYSTEM_BINARIES)

        return TrustedBinaries(dirs=dirs)

    def workspace(self, images: Path) -> WorkspaceSpec:
        """Workspace пользователя: шаблон программы, образ в каталоге images
        экземпляра, точка /workspace внутри песочницы."""
        mount = f"{images}/{WorkspaceSpec.USER_VAR}.ext4:{self.WORKSPACE_TARGET}"

        return WorkspaceSpec.model_validate(
            {"template": str(self.workspace_template()), "mount": mount}
        )


@dataclass(frozen=True)
class InstalledProgram(ProgramRoot):
    """Реализация ProgramRoot собранной Nuitka программы в образе и дереве
    релиза. Все части — в её каталоге."""

    def path(self, part: ProgramDir) -> Path:
        return self.root / part.value


@dataclass(frozen=True)
class SourceProgram(ProgramRoot):
    """Реализация ProgramRoot программы из исходников (.venv): то, что лежит в
    git, берётся по путям приложения относительно sources. Остальные части
    берутся из root — каталога собранных ресурсов."""

    sources: Path
    paths: Mapping[ProgramDir, Path] = field(default_factory=dict)

    def path(self, part: ProgramDir) -> Path:
        if source := self.paths.get(part):
            return self.sources / source

        return self.root / part.value


class ProgramRoots:
    """Поиск каталога программы на границе конфига: единственное место, где
    различаются собранная программа и программа из исходников.

    Собранная программа (Nuitka, `__compiled__`) лежит в
    <корень>/bin/<имя>.dist/<имя>.bin, и корень — третий предок бинарника;
    лаунчер песочницы и проба cgroup — её подкоманды (SandboxCommand).
    Программа на python бывает только из исходников: каталог runtime/
    репозитория называет переменная BOBA_RUNTIME, и рядом с ним обязано
    лежать дерево исходников (каталог packages). Создают его компоненты,
    которым каталог программы нужен: способ запуска плагинов, хранилище
    образов, вход чата.
    """

    RUNTIME_ENV: ClassVar[str] = "BOBA_RUNTIME"
    BINARY_DEPTH: ClassVar[int] = 2

    SOURCES_PROBE: ClassVar[tuple[str, ...]] = ("-c", "")
    """Пустой процесс интерпретатора исходников для пробы cgroup."""

    def __init__(self, source_paths: Mapping[ProgramDir, Path] | None = None) -> None:
        self._source_paths: dict[ProgramDir, Path] = {}
        if source_paths is not None:
            self._source_paths.update(source_paths)

    def compiled(self) -> bool:
        """Процесс — собранная программа: интерпретатора python у неё нет."""
        return "__compiled__" in globals()

    def locate(self) -> ProgramRoot:
        if self.compiled():
            return self._compiled()

        return self._python()

    def _compiled(self) -> ProgramRoot:
        binary = Path(sys.executable).resolve()
        root = binary.parents[self.BINARY_DEPTH]
        executable = str(binary)
        interpreter = LauncherInterpreter(
            executable=executable,
            env={},
            roots=(str(root),),
            launcher=(executable, SandboxCommand.LAUNCHER.value),
            probe=(executable, SandboxCommand.PROBE.value),
        )

        return InstalledProgram(root=root, interpreter=interpreter)

    def _python(self) -> ProgramRoot:
        runtime = os.environ.get(self.RUNTIME_ENV)
        if not runtime:
            msg = (
                f"program directory: the program runs on python from the "
                f"sources, so {self.RUNTIME_ENV} must name runtime/ of the "
                "repository; it is not set"
            )
            raise ConfigError(msg)

        root = Path(runtime).resolve()
        if not root.is_dir():
            msg = (
                f"program directory: {self.RUNTIME_ENV}={runtime} must name an "
                "existing directory laid out as a program, it does not exist"
            )
            raise ConfigError(msg)

        sources = root.parent
        if not (sources / SourceTree.PACKAGES.value).is_dir():
            msg = (
                f"program directory: {self.RUNTIME_ENV}={runtime} must be "
                f"runtime/ of the repository with {SourceTree.PACKAGES.value}/ "
                f"next to it, {sources} has none; an installed program is "
                "compiled and finds its directory by itself"
            )
            raise ConfigError(msg)

        return SourceProgram(
            root=root,
            interpreter=self._interpreter(),
            sources=sources,
            paths=self._source_paths,
        )

    def _interpreter(self) -> LauncherInterpreter:
        """Интерпретатор исходников со своим окружением и корнями.

        Текущий каталог и его предки в корни не идут: python -m кладёт cwd в
        sys.path, а cwd сервиса — дерево установки с конфигом и данными.
        """
        env: dict[LauncherEnv, str] = {}
        for name in LauncherEnv:
            value = os.environ.get(name.value)
            if not value:
                continue

            env[name] = value

        libraries = env.get(LauncherEnv.LD_LIBRARY_PATH, "").split(os.pathsep)
        # запуск относительным путём оставляет в sys.executable `..`: внутри
        # слоя такой путь не пройти, там нет промежуточных каталогов
        executable = os.path.abspath(sys.executable)
        candidates = (
            sys.base_prefix,
            sys.prefix,
            sys.exec_prefix,
            os.path.dirname(executable),
            os.path.dirname(os.path.realpath(executable)),
            *sys.path,
            *libraries,
        )

        cwd = os.getcwd()
        roots: list[str] = []
        for candidate in candidates:
            if not os.path.isabs(candidate):
                continue

            root = os.path.normpath(candidate)
            if self._holds(root, cwd):
                continue

            roots.append(root)

        # -P: корень слоя не встаёт первым в sys.path лаунчера
        launcher = (executable, "-P", "-m", LauncherChain.MODULE)

        return LauncherInterpreter(
            executable=executable,
            env=env,
            roots=tuple(dict.fromkeys(roots)),
            launcher=launcher,
            probe=(executable, *self.SOURCES_PROBE),
        )

    @staticmethod
    def _holds(root: str, path: str) -> bool:
        """root — сам path или его предок."""
        if path == root:
            return True

        return path.startswith(root.rstrip("/") + "/")


class WorkspaceData(StrEnum):
    """Каталог образов и рабочих директорий реализации песочницы."""

    WORKSPACE = "workspace"
