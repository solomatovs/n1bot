"""Tool: распаковка архивов (zip/docx/tar/tar.gz/tar.bz2/gz/bz2/xz)."""

from __future__ import annotations

import bz2
import gzip
import lzma
import posixpath
import tarfile
import zipfile
from io import BytesIO
from typing import Annotated, Literal

from pydantic import Field

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["unzip"]


class _Extractor:
    """Распаковка архивов разных форматов в workspace."""

    @staticmethod
    def run(
        shell: ProjectWorkspaceShell, src: str, dst: str, fmt: str,
    ) -> int:
        with shell.read_binary(src) as fh:
            data = fh.read()
        buf = BytesIO(data)
        if fmt == "zip":
            return _Extractor._zip(shell, buf, dst)
        if fmt == "tar":
            return _Extractor._tar(shell, buf, dst, mode="r:")
        if fmt == "tar.gz":
            return _Extractor._tar(shell, buf, dst, mode="r:gz")
        if fmt == "tar.bz2":
            return _Extractor._tar(shell, buf, dst, mode="r:bz2")
        if fmt == "gz":
            return _Extractor._single(shell, gzip.decompress(data), dst)
        if fmt == "bz2":
            return _Extractor._single(shell, bz2.decompress(data), dst)
        if fmt == "xz":
            return _Extractor._single(shell, lzma.decompress(data), dst)
        raise RuntimeError(f"Неизвестный формат: {fmt}")

    @staticmethod
    def _safe_join(dst: str, name: str) -> str:
        norm = posixpath.normpath(name)
        if norm.startswith(("..", "/")) or norm == "..":
            raise RuntimeError(f"Запрещённый путь в архиве: {name!r}")
        if norm in (".", ""):
            return dst
        return posixpath.join(dst, norm)

    @staticmethod
    def _ensure_parent(shell: ProjectWorkspaceShell, path: str) -> None:
        parent = posixpath.dirname(path)
        if parent:
            shell.mkdir(parent)

    @staticmethod
    def _zip(shell: ProjectWorkspaceShell, buf: BytesIO, dst: str) -> int:
        shell.mkdir(dst)
        count = 0
        with zipfile.ZipFile(buf) as zf:
            for info in zf.infolist():
                target = _Extractor._safe_join(dst, info.filename)
                if info.is_dir():
                    shell.mkdir(target)
                    continue
                _Extractor._ensure_parent(shell, target)
                with zf.open(info) as src_fh:
                    shell.atomic_write_binary(target, src_fh)
                count += 1
        return count

    @staticmethod
    def _tar(
        shell: ProjectWorkspaceShell,
        buf: BytesIO,
        dst: str,
        *,
        mode: Literal["r:", "r:gz", "r:bz2"],
    ) -> int:
        shell.mkdir(dst)
        count = 0
        with tarfile.open(fileobj=buf, mode=mode) as tf:
            for info in tf:
                if not (info.isfile() or info.isdir()):
                    continue
                target = _Extractor._safe_join(dst, info.name)
                if info.isdir():
                    shell.mkdir(target)
                    continue
                _Extractor._ensure_parent(shell, target)
                src_fh = tf.extractfile(info)
                if src_fh is None:
                    continue
                with src_fh:
                    shell.atomic_write_binary(target, src_fh)
                count += 1
        return count

    @staticmethod
    def _single(shell: ProjectWorkspaceShell, data: bytes, dst: str) -> int:
        _Extractor._ensure_parent(shell, dst)
        shell.atomic_write_binary(dst, BytesIO(data))
        return 1


@tool
def unzip(
    src: Annotated[str, Field(min_length=1, description="Путь к архиву.")],
    dst: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Путь назначения. Для zip/tar/tar.gz/tar.bz2 — директория "
                "(будет создана), для gz/bz2/xz — путь выходного файла."
            ),
        ),
    ],
    archive_format: Annotated[
        Literal["zip", "tar", "tar.gz", "tar.bz2", "gz", "bz2", "xz"],
        Field(
            description=(
                "Формат архива. 'zip' покрывает docx/xlsx/pptx (они zip). "
                "'gz'/'bz2'/'xz' — одиночные сжатые файлы."
            ),
        ),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> str:
    """Распаковать архив указанного формата в путь назначения.

    Symlink'и/спец-файлы в tar пропускаются. Запрещены пути, выходящие
    за пределы dst (path traversal).
    """
    try:
        count = _Extractor.run(shell, src, dst, archive_format)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Архив не найден: {src}") from e
    except (zipfile.BadZipFile, tarfile.TarError, lzma.LZMAError, OSError) as e:
        raise RuntimeError(f"Ошибка распаковки {src}: {e}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка записи: {e}") from e
    return f"Распаковано в {dst}: {count} файл(ов)"
