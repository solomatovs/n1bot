"""Файлы данных пакетов boba внутри собранного бинарника: опись сборки и
самопроверка по ней.

Стадия boba-bin запускает модуль интерпретатором над деревом колёс
(`python3 -m boba.cli.package_data <опись>`): PackageDataSurvey читает RECORD
установленных дистрибутивов boba-*, пишет опись их файлов данных и печатает
флаги Nuitka, которые кладут в бинарник эти файлы и саму опись. Команда
`boba self-check` бинарника (PackageDataCheck) читает опись из своих данных и
проверяет, что каждый файл описи доступен через importlib.resources так же,
как его читает код пакета.

Ошибки:
PackageDataError — опись не собрана или не читается, файл данных лежит вне
    обычного пакета, либо файлы описи в бинарнике недоступны.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Sequence
from enum import StrEnum
from importlib import metadata, resources
from importlib.machinery import all_suffixes
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "PackageDataCheck",
    "PackageDataError",
    "PackageDataManifest",
    "PackageDataSurvey",
    "PackageResource",
]


class PackageDataError(Exception):
    """Опись данных пакетов не собрана, не читается или не сходится с
    содержимым бинарника."""


class ManifestFile(StrEnum):
    """Опись внутри бинарника: пакет, в данных которого она лежит, её имя и
    кодировка."""

    PACKAGE = "boba.cli"
    NAME = "package_data.json"
    ENCODING = "utf-8"


class NuitkaFlag(StrEnum):
    """Флаги Nuitka, которыми данные пакетов попадают в бинарник."""

    PACKAGE_DATA = "--include-package-data="
    DATA_FILE = "--include-data-files="


class PackageResource(BaseModel):
    """Файл данных пакета: имя обычного пакета (с `__init__.py`), от
    которого его читает код, и путь внутри него."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str = Field(min_length=1)
    path: str = Field(min_length=1)

    def render(self) -> str:
        return f"{self.package}:{self.path}"


class PackageDataManifest(BaseModel):
    """Опись данных пакетов бинарника: что сборка обязалась положить внутрь."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resources: Sequence[PackageResource] = Field(min_length=1)

    def packages(self) -> Sequence[str]:
        """Пакеты описи по одному разу, по порядку имён."""
        found: set[str] = set()
        for resource in self.resources:
            found.add(resource.package)

        return sorted(found)


class PackageDataSurvey:
    """Опись данных пакетов дерева колёс для сборки бинарника.

    Источник — RECORD каждого установленного дистрибутива boba-*: всё, что
    колесо кладёт в каталог обычного пакета и что не модуль Python, — данные,
    которые код читает через importlib.resources. Создаётся входом модуля в
    стадии boba-bin; отдаёт опись и флаги Nuitka по ней."""

    PREFIX: ClassVar[str] = "boba-"
    INIT: ClassVar[str] = "__init__.py"
    CACHE: ClassVar[str] = "__pycache__"
    PARENT: ClassVar[str] = ".."
    METADATA_SUFFIX: ClassVar[str] = ".dist-info"

    def __init__(self) -> None:
        self._code_suffixes = tuple(all_suffixes())

    def manifest(self) -> PackageDataManifest:
        """Ошибки:
        PackageDataError — в дереве нет дистрибутивов boba-* с данными.
        """
        found = sorted(self._resources(), key=PackageResource.render)
        if not found:
            msg = (
                f"package data survey: the installed distributions {self.PREFIX}* "
                f"under {sys.path} carry no data files in their RECORD, expected at "
                "least the templates of boba-agent; is the wheel tree installed?"
            )
            raise PackageDataError(msg)

        return PackageDataManifest(resources=found)

    def flags(self, manifest: PackageDataManifest, written: Path) -> Iterator[str]:
        """Флаги Nuitka: данные каждого пакета описи и сама опись в данных
        boba.cli."""
        for package in manifest.packages():
            yield f"{NuitkaFlag.PACKAGE_DATA.value}{package}"

        package = PurePosixPath(*ManifestFile.PACKAGE.value.split("."))
        destination = package / ManifestFile.NAME.value

        yield f"{NuitkaFlag.DATA_FILE.value}{written}={destination}"

    def _resources(self) -> Iterator[PackageResource]:
        for dist in metadata.distributions():
            name = dist.metadata["Name"]
            if not name.lower().startswith(self.PREFIX):
                continue

            files = dist.files
            if files is None:
                msg = (
                    f"package data survey: distribution {name} has no RECORD, "
                    "expected it installed from a wheel"
                )
                raise PackageDataError(msg)

            yield from self._data_of(files)

    def _data_of(self, files: Iterable[metadata.PackagePath]) -> Iterator[PackageResource]:
        recorded: set[PurePosixPath] = set()
        for file in files:
            recorded.add(PurePosixPath(file.as_posix()))

        for path in sorted(recorded):
            if not self._data_candidate(path):
                continue

            anchor = self._anchor(path, recorded)
            if anchor is None:
                continue

            yield PackageResource(
                package=".".join(anchor.parts),
                path=str(path.relative_to(anchor)),
            )

    def _data_candidate(self, path: PurePosixPath) -> bool:
        if self.PARENT in path.parts:
            return False

        if self.CACHE in path.parts:
            return False

        if path.parts[0].endswith(self.METADATA_SUFFIX):
            return False

        return not path.name.endswith(self._code_suffixes)

    def _anchor(
        self, path: PurePosixPath, recorded: set[PurePosixPath]
    ) -> PurePosixPath | None:
        """Ближайший обычный пакет над файлом; None — файл не в пакете
        (скрипт, `.pth`, файл верхнего уровня)."""
        for parent in path.parents:
            if parent / self.INIT in recorded:
                return parent

        return None


class PackageDataCheck:
    """Самопроверка бинарника: каждый файл описи сборки доступен через
    importlib.resources от своего пакета. Ничего не пишет и никуда не ходит.
    Создаётся командой `boba self-check`; её зовут стадия boba-bin после
    компиляции, проверка образа (bobacheck) и тест бинарника."""

    def run(self) -> str:
        """Итог одной строкой.

        Ошибки:
        PackageDataError — описи нет или она не читается, либо часть её
            файлов в бинарнике недоступна.
        """
        manifest = self._manifest()
        missing = list(self._missing(manifest))
        if missing:
            total = len(manifest.resources)
            msg = (
                f"self-check: {len(missing)} of {total} package data files named by "
                f"the build manifest are not reachable inside this binary: "
                f"{'; '.join(missing)}. Rebuild with make -C build boba-bin: the "
                "build passes --include-package-data for every package of the "
                "manifest to Nuitka"
            )
            raise PackageDataError(msg)

        count = len(manifest.resources)
        packages = ", ".join(manifest.packages())

        return f"self-check: {count} package data files present ({packages})"

    def _manifest(self) -> PackageDataManifest:
        source = resources.files(ManifestFile.PACKAGE.value) / ManifestFile.NAME.value
        if not source.is_file():
            msg = (
                f"self-check: the package data manifest {source} is missing, expected "
                "it inside a binary built by make -C build boba-bin; a run from the "
                "source tree has no manifest"
            )
            raise PackageDataError(msg)

        text = source.read_text(encoding=ManifestFile.ENCODING.value)
        try:
            return PackageDataManifest.model_validate_json(text)
        except ValidationError as exc:
            msg = f"self-check: the package data manifest {source} is not valid: {exc}"
            raise PackageDataError(msg) from exc

    def _missing(self, manifest: PackageDataManifest) -> Iterator[str]:
        for resource in manifest.resources:
            try:
                root = resources.files(resource.package)
            except ModuleNotFoundError as exc:
                yield f"{resource.render()} (package not in the binary: {exc})"
                continue

            if root.joinpath(resource.path).is_file():
                continue

            yield f"{resource.render()} (no file under {root})"


class SurveyEntry:
    """Вход модуля в стадии boba-bin: опись в файл аргумента, флаги Nuitka
    построчно в stdout."""

    def __init__(self) -> None:
        self._survey = PackageDataSurvey()

    def run(self, argv: Sequence[str]) -> int:
        if len(argv) != 1:
            print(
                "usage: python3 -m boba.cli.package_data <manifest.json>",
                file=sys.stderr,
            )
            return 2

        written = Path(argv[0])
        try:
            manifest = self._survey.manifest()
        except PackageDataError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        written.write_text(
            manifest.model_dump_json(indent=2), encoding=ManifestFile.ENCODING.value
        )
        for flag in self._survey.flags(manifest, written):
            print(flag)

        return 0


if __name__ == "__main__":
    sys.exit(SurveyEntry().run(sys.argv[1:]))
