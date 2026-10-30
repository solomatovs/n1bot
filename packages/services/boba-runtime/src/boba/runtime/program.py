"""Собранная программа boba (Nuitka): служебные подкоманды, опись сборки и
самопроверка по ней.

Стадия сборки бинарника запускает модуль интерпретатором над деревом колёс
(`python3 -m boba.runtime.program <дерево> <опись>`): ProgramSurvey читает
RECORD и entry points дистрибутивов boba-*, установленных в дерево, пишет
опись и печатает флаги Nuitka, которые кладут в бинарник файлы данных
пакетов, метаданные дистрибутивов с entry points, модули их целей и саму
опись. Подкоманда `self-check` бинарника (ProgramCheck) читает опись из своих
данных и проверяет, что каждый файл описи доступен через importlib.resources
так же, как его читает код пакета, а каждый entry point находится в своей
группе и загружается.

Ошибки:
ProgramManifestError — опись не собрана или не читается, либо файлы или
    entry points описи в бинарнике недоступны.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Sequence
from enum import IntEnum, StrEnum
from importlib import metadata, resources
from importlib.machinery import all_suffixes
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "EntryPointRef",
    "PackageResource",
    "ProgramCheck",
    "ProgramExit",
    "ProgramManifest",
    "ProgramManifestError",
    "ProgramSurvey",
    "ProgramTool",
]


class ProgramManifestError(Exception):
    """Опись сборки не собрана, не читается или не сходится с содержимым
    бинарника."""


class ProgramTool(StrEnum):
    """Служебные подкоманды собранной программы: идут без службы и её
    импортов. launcher и probe зовёт песочница (команды собирает ProgramRoots),
    self-check — сборка после компиляции и проверка образа."""

    LAUNCHER = "launcher"
    PROBE = "probe"
    SELF_CHECK = "self-check"


class ManifestFile(StrEnum):
    """Опись внутри бинарника: пакет, в данных которого она лежит, её имя и
    кодировка."""

    PACKAGE = "boba.runtime"
    NAME = "program.json"
    ENCODING = "utf-8"


class WheelLayout(StrEnum):
    """Имена дерева установки, по которым опись отличает данные пакета от
    кода, метаданных и файлов для проверки типов, а свои группы entry points
    — от чужих (console_scripts)."""

    DIST_PREFIX = "boba-"
    GROUP_PREFIX = "boba."
    INIT = "__init__.py"
    CACHE = "__pycache__"
    PARENT = ".."
    METADATA_SUFFIX = ".dist-info"
    TYPED_MARKER = "py.typed"
    STUB_SUFFIX = ".pyi"


class NuitkaFlag(StrEnum):
    """Флаги Nuitka, которыми данные пакетов и entry points попадают в
    бинарник."""

    MODULE = "--include-module="
    PACKAGE_DATA = "--include-package-data="
    DATA_FILE = "--include-data-files="
    METADATA = "--include-distribution-metadata="


class PackageResource(BaseModel):
    """Файл данных пакета: имя обычного пакета (с `__init__.py`), от
    которого его читает код, и путь внутри него."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str = Field(min_length=1)
    path: str = Field(min_length=1)

    def render(self) -> str:
        return f"{self.package}:{self.path}"


class EntryPointRef(BaseModel):
    """Entry point группы boba.* дистрибутива дерева колёс: код находит его
    по группе и имени (importlib.metadata.entry_points) и загружает цель."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    distribution: str = Field(min_length=1)
    group: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)

    def entry(self) -> metadata.EntryPoint:
        return metadata.EntryPoint(name=self.name, value=self.value, group=self.group)

    def render(self) -> str:
        return f"{self.group}:{self.name} = {self.value} ({self.distribution})"


class ProgramManifest(BaseModel):
    """Опись бинарника: что сборка обязалась положить внутрь."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resources: Sequence[PackageResource] = Field(min_length=1)
    entry_points: Sequence[EntryPointRef]

    def packages(self) -> Sequence[str]:
        """Пакеты описи по одному разу, по порядку имён."""
        found: set[str] = set()
        for resource in self.resources:
            found.add(resource.package)

        return sorted(found)

    def distributions(self) -> Sequence[str]:
        """Дистрибутивы с entry points по одному разу, по порядку имён."""
        found: set[str] = set()
        for entry in self.entry_points:
            found.add(entry.distribution)

        return sorted(found)

    def modules(self) -> Sequence[str]:
        """Модули целей entry points по одному разу, по порядку имён."""
        found: set[str] = set()
        for entry in self.entry_points:
            found.add(entry.entry().module)

        return sorted(found)

    def groups(self) -> Sequence[str]:
        found: set[str] = set()
        for entry in self.entry_points:
            found.add(entry.group)

        return sorted(found)


class ProgramSurvey:
    """Опись дерева колёс для сборки бинарника.

    Источник — RECORD и entry points каждого дистрибутива boba-*,
    установленного в дерево колёс (только в него: дистрибутивы
    интерпретатора и `.egg-info` исходников в опись не попадают). Данные —
    всё, что колесо кладёт в каталог обычного пакета и что не модуль Python и
    не файл для проверки типов: их код читает через importlib.resources.
    Entry points — группы boba.*: их код ищет по метаданным дистрибутива и
    загружает импортом, которого Nuitka сама не видит. Создаётся входом
    модуля (SurveyEntry) в стадии сборки бинарника; отдаёт опись и флаги
    Nuitka по ней."""

    def __init__(self) -> None:
        self._skipped_suffixes = (*all_suffixes(), WheelLayout.STUB_SUFFIX.value)

    def manifest(self, site: Path) -> ProgramManifest:
        """Ошибки:
        ProgramManifestError — в дереве нет дистрибутивов boba-* с данными
            или у дистрибутива нет RECORD.
        """
        found = sorted(self._resources(site), key=PackageResource.render)
        if not found:
            prefix = WheelLayout.DIST_PREFIX.value
            msg = (
                f"program survey: the distributions {prefix}* installed in "
                f"{site} carry no data files in their RECORD, expected at "
                "least the SQL schemas of boba-runtime; is the wheel tree installed there?"
            )
            raise ProgramManifestError(msg)

        entries = sorted(self._entry_points(site), key=EntryPointRef.render)

        return ProgramManifest(resources=found, entry_points=entries)

    def flags(self, manifest: ProgramManifest, written: Path) -> Iterator[str]:
        """Флаги Nuitka: модуль каждого пакета описи (иначе пакет, которого
        не импортирует код бинарника, есть только каталогом данных, и
        importlib.resources его не найдёт) и его данные; метаданные
        дистрибутивов с entry points и модули их целей; сама опись в данных
        boba.runtime."""
        for package in manifest.packages():
            yield f"{NuitkaFlag.MODULE.value}{package}"
            yield f"{NuitkaFlag.PACKAGE_DATA.value}{package}"

        for distribution in manifest.distributions():
            yield f"{NuitkaFlag.METADATA.value}{distribution}"

        for module in manifest.modules():
            yield f"{NuitkaFlag.MODULE.value}{module}"

        package = PurePosixPath(*ManifestFile.PACKAGE.value.split("."))
        destination = package / ManifestFile.NAME.value

        yield f"{NuitkaFlag.DATA_FILE.value}{written}={destination}"

    def _boba(self, site: Path) -> Iterator[metadata.Distribution]:
        for dist in metadata.distributions(path=[str(site)]):
            name = dist.metadata["Name"]
            if not name.lower().startswith(WheelLayout.DIST_PREFIX.value):
                continue

            yield dist

    def _resources(self, site: Path) -> Iterator[PackageResource]:
        for dist in self._boba(site):
            files = dist.files
            if files is None:
                msg = (
                    f"program survey: distribution {dist.metadata['Name']} in {site} "
                    "has no RECORD, expected it installed from a wheel"
                )
                raise ProgramManifestError(msg)

            yield from self._data_of(files)

    def _entry_points(self, site: Path) -> Iterator[EntryPointRef]:
        for dist in self._boba(site):
            for entry in dist.entry_points:
                if not entry.group.startswith(WheelLayout.GROUP_PREFIX.value):
                    continue

                yield EntryPointRef(
                    distribution=dist.metadata["Name"],
                    group=entry.group,
                    name=entry.name,
                    value=entry.value,
                )

    def _data_of(
        self, files: Iterable[metadata.PackagePath]
    ) -> Iterator[PackageResource]:
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
        if WheelLayout.PARENT.value in path.parts:
            return False

        if WheelLayout.CACHE.value in path.parts:
            return False

        if path.parts[0].endswith(WheelLayout.METADATA_SUFFIX.value):
            return False

        if path.name == WheelLayout.TYPED_MARKER.value:
            return False

        return not path.name.endswith(self._skipped_suffixes)

    def _anchor(
        self, path: PurePosixPath, recorded: set[PurePosixPath]
    ) -> PurePosixPath | None:
        """Ближайший обычный пакет над файлом; None — файл не в пакете
        (скрипт, `.pth`, файл верхнего уровня)."""
        for parent in path.parents:
            if parent / WheelLayout.INIT.value in recorded:
                return parent

        return None


class ProgramCheck:
    """Самопроверка бинарника: каждый файл описи сборки доступен через
    importlib.resources от своего пакета, каждый entry point описи находится
    в своей группе и загружается. Ничего не пишет и никуда не ходит.
    Создаётся подкомандой `self-check` программ boba и агента; её зовут
    стадия сборки после компиляции, проверка образа (bobacheck) и тест
    бинарника."""

    def run(self) -> str:
        """Итог одной строкой.

        Ошибки:
        ProgramManifestError — описи нет или она не читается, либо часть её
            файлов или entry points в бинарнике недоступна.
        """
        manifest = self._manifest()
        missing = list(self._missing(manifest))
        if missing:
            total = len(manifest.resources)
            msg = (
                f"self-check: {len(missing)} of {total} package data files named by "
                f"the build manifest are not reachable inside this binary: "
                f"{'; '.join(missing)}. Rebuild the program: the build passes "
                "--include-package-data for every package of the manifest to Nuitka"
            )
            raise ProgramManifestError(msg)

        broken = list(self._broken(manifest))
        if broken:
            total = len(manifest.entry_points)
            msg = (
                f"self-check: {len(broken)} of {total} entry points named by the "
                f"build manifest do not load inside this binary: {'; '.join(broken)}. "
                "Rebuild the program: the build passes --include-distribution-metadata "
                "and --include-module for every entry point of the manifest to Nuitka"
            )
            raise ProgramManifestError(msg)

        count = len(manifest.resources)
        packages = ", ".join(manifest.packages())
        entries = len(manifest.entry_points)
        groups = ", ".join(manifest.groups())

        return (
            f"self-check: {count} package data files present ({packages}); "
            f"{entries} entry points load ({groups})"
        )

    def _manifest(self) -> ProgramManifest:
        source = resources.files(ManifestFile.PACKAGE.value) / ManifestFile.NAME.value
        if not source.is_file():
            msg = (
                f"self-check: the build manifest {source} is missing, expected "
                "it inside a binary built by make -C build; a run from the source "
                "tree has no manifest"
            )
            raise ProgramManifestError(msg)

        text = source.read_text(encoding=ManifestFile.ENCODING.value)
        try:
            return ProgramManifest.model_validate_json(text)
        except ValidationError as exc:
            msg = f"self-check: the build manifest {source} is not valid: {exc}"
            raise ProgramManifestError(msg) from exc

    def _missing(self, manifest: ProgramManifest) -> Iterator[str]:
        for resource in manifest.resources:
            try:
                root = resources.files(resource.package)
            except ModuleNotFoundError as exc:
                yield f"{resource.render()} (package not in the binary: {exc})"
                continue

            parts = PurePosixPath(resource.path).parts
            if root.joinpath(*parts).is_file():
                continue

            yield f"{resource.render()} (no file under {root})"

    def _broken(self, manifest: ProgramManifest) -> Iterator[str]:
        for expected in manifest.entry_points:
            found = metadata.entry_points(group=expected.group, name=expected.name)
            if not found:
                yield (
                    f"{expected.render()} (no such entry point: the distribution "
                    "metadata is not in the binary)"
                )
                continue

            for entry in found:
                try:
                    entry.load()
                except (ImportError, AttributeError) as exc:
                    yield f"{expected.render()} (load failed: {exc})"


class ProgramExit(IntEnum):
    """Коды выхода входа модуля и служебных подкоманд программы."""

    OK = 0
    FAILED = 1
    USAGE = 2


class SurveyEntry:
    """Вход модуля в стадии сборки бинарника: дерево колёс и путь описи —
    аргументами; опись в файл, флаги Nuitka построчно в stdout."""

    USAGE: ClassVar[str] = (
        "usage: python3 -m boba.runtime.program <site-packages dir> <manifest.json>\n"
    )

    def __init__(self) -> None:
        self._survey = ProgramSurvey()

    def run(self, argv: Sequence[str]) -> int:
        match argv:
            case [site, written]:
                return self._write(Path(site), Path(written))
            case _:
                sys.stderr.write(self.USAGE)
                return ProgramExit.USAGE

    def _write(self, site: Path, written: Path) -> int:
        try:
            manifest = self._survey.manifest(site)
        except ProgramManifestError as exc:
            sys.stderr.write(f"{exc}\n")
            return ProgramExit.FAILED

        text = manifest.model_dump_json(indent=2)
        try:
            written.write_text(text, encoding=ManifestFile.ENCODING.value)
        except OSError as exc:
            sys.stderr.write(
                f"program survey: writing the manifest {written} failed: {exc}\n"
            )
            return ProgramExit.FAILED

        for flag in self._survey.flags(manifest, written):
            sys.stdout.write(f"{flag}\n")

        return ProgramExit.OK


if __name__ == "__main__":
    sys.exit(SurveyEntry().run(sys.argv[1:]))
