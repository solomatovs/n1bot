"""Файлы данных пакетов boba и их entry points внутри собранного бинарника:
опись сборки и самопроверка по ней.

Стадия сборки бинарника (агент boba, программа плагина) запускает модуль
интерпретатором над деревом колёс (`python3 -m boba.core.package_data
<дерево> <опись> [<префикс групп>]`): PackageDataSurvey читает RECORD и
entry_points.txt дистрибутивов boba-*, установленных в дерево, пишет опись их
файлов данных и entry points групп с данным префиксом и печатает флаги
Nuitka, которые кладут в бинарник эти файлы, метаданные дистрибутивов, модули
entry points и саму опись. Самопроверка бинарника (PackageDataCheck) читает
опись из своих данных и проверяет, что каждый файл описи доступен через
importlib.resources так же, как его читает код пакета, а каждый entry point
находится importlib.metadata и загружается.

Ошибки:
PackageDataError — опись не собрана или не читается, либо файлы и entry
    points описи в бинарнике недоступны.
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
    "PackageDataCheck",
    "PackageDataError",
    "PackageDataManifest",
    "PackageDataSurvey",
    "PackageEntryPoint",
    "PackageResource",
]


class PackageDataError(Exception):
    """Опись данных пакетов не собрана, не читается или не сходится с
    содержимым бинарника."""


class ManifestFile(StrEnum):
    """Опись внутри бинарника: пакет, в данных которого она лежит, её имя и
    кодировка."""

    PACKAGE = "boba.core"
    NAME = "package_data.json"
    ENCODING = "utf-8"


class WheelLayout(StrEnum):
    """Имена дерева установки, по которым опись отличает данные пакета от
    кода, метаданных и файлов для проверки типов."""

    DIST_PREFIX = "boba-"
    INIT = "__init__.py"
    CACHE = "__pycache__"
    PARENT = ".."
    METADATA_SUFFIX = ".dist-info"
    TYPED_MARKER = "py.typed"
    STUB_SUFFIX = ".pyi"


class NuitkaFlag(StrEnum):
    """Флаги Nuitka, которыми данные пакетов и entry points попадают в бинарник."""

    PACKAGE = "--include-module="
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


class PackageEntryPoint(BaseModel):
    """Entry point дистрибутива boba: код бинарника находит его поиском
    importlib.metadata по группе, а не импортом."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    distribution: str = Field(min_length=1)
    group: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)

    def module(self) -> str:
        return self.value.partition(":")[0].strip()

    def render(self) -> str:
        return f"{self.group}:{self.name}"


class PackageDataManifest(BaseModel):
    """Опись бинарника: какие файлы данных и entry points сборка обязалась
    положить внутрь."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resources: Sequence[PackageResource] = ()
    entry_points: Sequence[PackageEntryPoint] = ()

    def packages(self) -> Sequence[str]:
        """Пакеты с данными по одному разу, по порядку имён."""
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
        """Модули entry points по одному разу, по порядку имён."""
        found: set[str] = set()
        for entry in self.entry_points:
            found.add(entry.module())

        return sorted(found)

    def groups(self) -> Sequence[str]:
        found: set[str] = set()
        for entry in self.entry_points:
            found.add(entry.group)

        return sorted(found)


class PackageDataSurvey:
    """Опись дерева колёс для сборки бинарника.

    Источник — дистрибутивы boba-*, установленные в дерево колёс (только в
    него: дистрибутивы интерпретатора и `.egg-info` исходников в опись не
    попадают). Данные — всё, что колесо кладёт по RECORD в каталог обычного
    пакета и что не модуль Python и не файл для проверки типов: их код читает
    через importlib.resources. Entry points — группы с префиксом, который
    назвала сборка: без метаданных дистрибутива поиск по группе внутри
    бинарника пуст. Создаётся входом модуля (SurveyEntry) в стадии сборки
    бинарника; отдаёт опись и флаги Nuitka по ней."""

    def __init__(self) -> None:
        self._skipped_suffixes = (*all_suffixes(), WheelLayout.STUB_SUFFIX.value)

    def manifest(self, site: Path, group_prefix: str) -> PackageDataManifest:
        """group_prefix — префикс групп entry points описи; пусто — без них.

        Ошибки:
        PackageDataError — в дереве нет дистрибутивов boba-* или у
            дистрибутива нет RECORD.
        """
        dists = list(self._boba_dists(site))
        if not dists:
            prefix = WheelLayout.DIST_PREFIX.value
            msg = (
                f"package data survey: no distributions {prefix}* are installed in "
                f"{site}, expected the wheel tree of the binary there"
            )
            raise PackageDataError(msg)

        resources_found: list[PackageResource] = []
        entries_found: list[PackageEntryPoint] = []
        for dist in dists:
            resources_found.extend(self._resources_of(dist, site))
            if group_prefix:
                entries_found.extend(self._entry_points_of(dist, group_prefix))

        resources_found.sort(key=PackageResource.render)
        entries_found.sort(key=PackageEntryPoint.render)

        return PackageDataManifest(
            resources=resources_found, entry_points=entries_found
        )

    def flags(self, manifest: PackageDataManifest, written: Path) -> Iterator[str]:
        """Флаги Nuitka: модуль каждого пакета с данными (иначе пакет, которого
        не импортирует код бинарника, есть только каталогом данных, и
        importlib.resources его не найдёт) и его данные; метаданные
        дистрибутивов и модули их entry points; сама опись в данных boba.core."""
        for package in manifest.packages():
            yield f"{NuitkaFlag.PACKAGE.value}{package}"
            yield f"{NuitkaFlag.PACKAGE_DATA.value}{package}"

        for distribution in manifest.distributions():
            yield f"{NuitkaFlag.METADATA.value}{distribution}"

        for module in manifest.modules():
            yield f"{NuitkaFlag.PACKAGE.value}{module}"

        package = PurePosixPath(*ManifestFile.PACKAGE.value.split("."))
        destination = package / ManifestFile.NAME.value

        yield f"{NuitkaFlag.DATA_FILE.value}{written}={destination}"

    def _boba_dists(self, site: Path) -> Iterator[metadata.Distribution]:
        for dist in metadata.distributions(path=[str(site)]):
            name = dist.metadata["Name"]
            if not name.lower().startswith(WheelLayout.DIST_PREFIX.value):
                continue

            yield dist

    def _resources_of(
        self, dist: metadata.Distribution, site: Path
    ) -> Iterator[PackageResource]:
        files = dist.files
        if files is None:
            name = dist.metadata["Name"]
            msg = (
                f"package data survey: distribution {name} in {site} has "
                "no RECORD, expected it installed from a wheel"
            )
            raise PackageDataError(msg)

        yield from self._data_of(files)

    def _entry_points_of(
        self, dist: metadata.Distribution, group_prefix: str
    ) -> Iterator[PackageEntryPoint]:
        name = dist.metadata["Name"]
        for entry in dist.entry_points:
            if not entry.group.startswith(group_prefix):
                continue

            yield PackageEntryPoint(
                distribution=name, group=entry.group, name=entry.name, value=entry.value
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


class PackageDataCheck:
    """Самопроверка бинарника: каждый файл описи сборки доступен через
    importlib.resources от своего пакета, каждый entry point описи находится
    поиском по группе и загружается. Ничего не пишет и никуда не ходит.
    Создаётся командой самопроверки бинарника (`boba self-check`, `/plugin/run
    self-check`); её зовут стадия сборки после компиляции, проверка образа
    (bobacheck) и тест бинарника."""

    def run(self) -> str:
        """Итог одной строкой.

        Ошибки:
        PackageDataError — описи нет или она не читается, либо часть её
            файлов или entry points в бинарнике недоступна.
        """
        manifest = self._manifest()
        missing = [*self._missing(manifest), *self._unreachable(manifest)]
        if missing:
            total = len(manifest.resources) + len(manifest.entry_points)
            msg = (
                f"self-check: {len(missing)} of {total} package data files and entry "
                f"points named by the build manifest are not reachable inside this "
                f"binary: {'; '.join(missing)}. Rebuild the binary: the build passes "
                "--include-package-data, --include-distribution-metadata and "
                "--include-module for every entry of the manifest to Nuitka"
            )
            raise PackageDataError(msg)

        count = len(manifest.resources)
        packages = ", ".join(manifest.packages())
        checked = f"self-check: {count} package data files present ({packages})"
        if not manifest.entry_points:
            return checked

        entries = len(manifest.entry_points)
        groups = ", ".join(manifest.groups())

        return f"{checked}, {entries} entry points load ({groups})"

    def _manifest(self) -> PackageDataManifest:
        source = resources.files(ManifestFile.PACKAGE.value) / ManifestFile.NAME.value
        if not source.is_file():
            msg = (
                f"self-check: the package data manifest {source} is missing, expected "
                "it inside a binary built by make -C build; a run from the "
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

            parts = PurePosixPath(resource.path).parts
            if root.joinpath(*parts).is_file():
                continue

            yield f"{resource.render()} (no file under {root})"

    def _unreachable(self, manifest: PackageDataManifest) -> Iterator[str]:
        for expected in manifest.entry_points:
            group = expected.group
            found = tuple(metadata.entry_points(group=group, name=expected.name))
            if not found:
                yield (
                    f"{expected.render()} (no entry point: metadata of "
                    f"{expected.distribution} is not in the binary)"
                )
                continue

            try:
                found[0].load()
            except (ImportError, AttributeError) as exc:
                yield f"{expected.render()} ({expected.value} does not load: {exc})"


class SurveyExit(IntEnum):
    """Коды выхода входа модуля."""

    OK = 0
    FAILED = 1
    USAGE = 2


class SurveyEntry:
    """Вход модуля в стадии сборки бинарника: дерево колёс, путь описи и
    префикс групп entry points (пусто — без них) — аргументами; опись в файл,
    флаги Nuitka построчно в stdout."""

    USAGE: ClassVar[str] = (
        "usage: python3 -m boba.core.package_data <site-packages dir> <manifest.json>"
        " [<entry point group prefix>]\n"
    )

    def __init__(self) -> None:
        self._survey = PackageDataSurvey()

    def run(self, argv: Sequence[str]) -> int:
        match argv:
            case [site, written]:
                return self._write(Path(site), Path(written), "")
            case [site, written, group_prefix]:
                return self._write(Path(site), Path(written), group_prefix)
            case _:
                sys.stderr.write(self.USAGE)
                return SurveyExit.USAGE

    def _write(self, site: Path, written: Path, group_prefix: str) -> int:
        try:
            manifest = self._survey.manifest(site, group_prefix)
        except PackageDataError as exc:
            sys.stderr.write(f"{exc}\n")
            return SurveyExit.FAILED

        text = manifest.model_dump_json(indent=2)
        try:
            written.write_text(text, encoding=ManifestFile.ENCODING.value)
        except OSError as exc:
            sys.stderr.write(
                f"package data survey: writing the manifest {written} failed: {exc}\n"
            )
            return SurveyExit.FAILED

        for flag in self._survey.flags(manifest, written):
            sys.stdout.write(f"{flag}\n")

        return SurveyExit.OK


if __name__ == "__main__":
    sys.exit(SurveyEntry().run(sys.argv[1:]))
