"""Версия пакетов репозитория: одна на все pyproject.toml и пины boba-*.

Вызов: version.py <команда> <корень packages> [аргумент]
  show      — напечатать версию
  requires  — напечатать минимальный python (requires-python)
  check     — упасть, если версии разошлись или пин boba-* смотрит на другую версию
  set X.Y.Z — проставить версию и пины во все пакеты
  python-check X.Y — упасть, если python X.Y младше requires-python пакетов
"""

import re
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Command(StrEnum):
    SHOW = "show"
    REQUIRES = "requires"
    CHECK = "check"
    SET = "set"
    PYTHON_CHECK = "python-check"


@dataclass(frozen=True)
class Project:
    path: Path
    version: str
    requires_python: str
    pins: tuple[str, ...]


class Repository:
    """Пакеты репозитория по их pyproject.toml."""

    SKIP = ("/build/", "/.venv/", "/node_modules/")
    VERSION_LINE = re.compile(r'^version = "(.*)"$', re.MULTILINE)
    PIN = re.compile(r'"boba-[a-z0-9-]+(?:\[[a-z0-9,_-]+\])?==([^"]+)"')
    NEW_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$")

    def __init__(self, root: Path) -> None:
        self._projects = list(self._load(root))
        if not self._projects:
            raise SystemExit(f"no pyproject.toml under {root}")

    def _load(self, root: Path) -> Iterator[Project]:
        for path in sorted(root.rglob("pyproject.toml")):
            if any(part in str(path) for part in self.SKIP):
                continue

            text = path.read_text(encoding="utf-8")
            project = tomllib.loads(text).get("project", {})
            yield Project(
                path=path,
                version=project.get("version", ""),
                requires_python=project.get("requires-python", ""),
                pins=tuple(self.PIN.findall(text)),
            )

    @property
    def version(self) -> str:
        versions = sorted({project.version for project in self._projects})
        if len(versions) != 1:
            raise SystemExit(f"package versions diverged: {', '.join(versions)}")

        return versions[0]

    @property
    def requires_python(self) -> str:
        found = re.match(r">=([0-9]+\.[0-9]+)", self._projects[0].requires_python)
        if found is None:
            msg = f"requires-python is not '>=X.Y' in {self._projects[0].path}"
            raise SystemExit(msg)

        return found.group(1)

    def python_check(self, candidate: str) -> str:
        required = self._parse_python(self.requires_python)
        given = self._parse_python(candidate)
        if given < required:
            raise SystemExit(
                f"python {candidate} is older than requires-python >={self.requires_python}"
            )

        return f"python-check: {candidate} >= {self.requires_python} - ok"

    def _parse_python(self, text: str) -> tuple[int, int]:
        found = re.fullmatch(r"([0-9]+)\.([0-9]+)", text)
        if found is None:
            raise SystemExit(f"python version must be X.Y, not '{text}'")

        return int(found.group(1)), int(found.group(2))

    def check(self) -> str:
        version = self.version
        for project in self._projects:
            for pin in project.pins:
                if pin != version:
                    msg = f"{project.path}: boba-* pinned to {pin}, expected {version}"
                    raise SystemExit(msg)

        return f"version-check: {version}, {len(self._projects)} packages - ok"

    def set(self, new: str) -> str:
        if self.NEW_VERSION.match(new) is None:
            raise SystemExit(f"version must be X.Y.Z or X.Y.Z.devN, not '{new}'")

        old = self.version
        for project in self._projects:
            text = project.path.read_text(encoding="utf-8")
            text = self.VERSION_LINE.sub(f'version = "{new}"', text, count=1)
            text = text.replace(f'=={old}"', f'=={new}"')
            project.path.write_text(text, encoding="utf-8")

        return f">>> {old} -> {new} in {len(self._projects)} packages"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    command = Command(argv[1])
    repository = Repository(Path(argv[2]))

    if command is Command.SHOW:
        print(repository.version)
        return 0

    if command is Command.REQUIRES:
        print(repository.requires_python)
        return 0

    if command is Command.CHECK:
        print(repository.check())
        return 0

    if len(argv) < 4:
        raise SystemExit(f"usage: version.py {command} <packages root> <argument>")

    if command is Command.PYTHON_CHECK:
        print(repository.python_check(argv[3]))
        return 0

    print(repository.set(argv[3]))
    print(Repository(Path(argv[2])).check())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
