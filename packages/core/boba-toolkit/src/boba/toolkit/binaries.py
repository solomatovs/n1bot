"""Резолв исполняемых файлов песочницы по списку доверенных каталогов.

$PATH не читается: кто им управляет, тот подменяет bwrap и fuse2fs, а это
полный обход изоляции (CWE-426).

Ошибки: UntrustedBinaryError — бинаря нет в доверенных каталогах либо путь
к нему доступен на запись посторонним.
"""

from __future__ import annotations

import os
import stat as stat_module
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["SandboxBinary", "TrustedBinaries", "UntrustedBinaryError"]


class UntrustedBinaryError(RuntimeError):
    """Исполняемый файл не найден в доверенных каталогах или небезопасен."""


class SandboxBinary(StrEnum):
    """Внешние исполняемые файлы, которые запускает песочница."""

    BWRAP = "bwrap"
    FUSE2FS = "fuse2fs"


class TrustedBinaries(BaseModel):
    """Каталоги, из которых разрешено брать исполняемые файлы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dirs: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Абсолютные каталоги поиска в порядке приоритета; $PATH "
            "игнорируется. Несуществующий каталог пропускается."
        ),
    )

    @field_validator("dirs", mode="after")
    @classmethod
    def _canonicalize(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical: list[str] = []

        for item in value:
            if not item.startswith("/"):
                msg = f"trusted binaries: dir must be absolute, got {item!r}"
                raise ValueError(msg)

            canonical.append(os.path.normpath(item))

        return tuple(canonical)

    def resolve(self, binary: SandboxBinary) -> str:
        """Абсолютный путь до бинаря; иначе UntrustedBinaryError."""
        return self.resolve_any(binary)

    def resolve_any(self, *binaries: SandboxBinary) -> str:
        """Первый найденный из перечисленных, в порядке приоритета."""
        if not binaries:
            msg = "trusted binaries: no binary requested"
            raise UntrustedBinaryError(msg)

        for binary in binaries:
            found = self._locate(binary)
            if found is not None:
                return found

        names: list[str] = []
        for binary in binaries:
            names.append(binary.value)

        msg = (
            f"trusted binaries: {'/'.join(names)} not found in "
            f"{', '.join(self.dirs)}"
        )
        raise UntrustedBinaryError(msg)

    def has(self, binary: SandboxBinary) -> bool:
        """Доступен ли бинарь: проверка предпосылок без падения."""
        return self._locate(binary) is not None

    def _locate(self, binary: SandboxBinary) -> str | None:
        for directory in self.dirs:
            candidate = os.path.join(directory, binary.value)
            if not self._executable(candidate):
                continue

            self._check_trusted(candidate, directory)
            return candidate

        return None

    @staticmethod
    def _executable(path: str) -> bool:
        if not os.path.isfile(path):
            return False

        return os.access(path, os.X_OK)

    @classmethod
    def _check_trusted(cls, path: str, root: str) -> None:
        """Проверка идёт от файла вверх до объявленного каталога включительно.

        Выше root не поднимаемся: этот каталог администратор назвал доверенным
        сам, а права над ним — часть развёртывания, а не выбора бинаря.
        """
        real = os.path.realpath(path)
        cls._check_protected(real)
        cls._check_protected(root)

        directory = os.path.dirname(real)
        while directory != root:
            parent = os.path.dirname(directory)
            if parent == directory:
                return

            cls._check_protected(directory)
            directory = parent

    @classmethod
    def _check_protected(cls, path: str) -> None:
        """Владельца не проверяем: в userns хостовые uid видны как nobody."""
        try:
            info = os.stat(path)
        except OSError as exc:
            msg = f"trusted binaries: cannot stat {path}: {exc}"
            raise UntrustedBinaryError(msg) from exc

        # sticky-каталог (/tmp) открыт на запись, но чужой файл в нём не подменить
        if cls._is_sticky_dir(info.st_mode):
            return

        if info.st_mode & stat_module.S_IWOTH:
            msg = f"trusted binaries: {path} is world-writable"
            raise UntrustedBinaryError(msg)

        if info.st_mode & stat_module.S_IWGRP:
            msg = f"trusted binaries: {path} is group-writable"
            raise UntrustedBinaryError(msg)

    @staticmethod
    def _is_sticky_dir(mode: int) -> bool:
        if not stat_module.S_ISDIR(mode):
            return False

        return bool(mode & stat_module.S_ISVTX)
