"""Версия пакетов репозитория: одна на все pyproject.toml, внутренние пины boba-* — на неё же.

Вызов: version.py <команда> <корень packages> [аргумент]
  show      — напечатать версию
  requires  — напечатать минимальный python (requires-python)
  check     — упасть, если версии разошлись или пин boba-* смотрит на другую версию
  set X.Y.Z — проставить версию и пины во все пакеты
"""

import re
import sys
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterator


class Command(StrEnum):
    SHOW = "show"
    REQUIRES = "requires"
    CHECK = "check"
    SET = "set"


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
            raise SystemExit(f"requires-python is not '>=X.Y' in {self._projects[0].path}")

        return found.group(1)

    def check(self) -> str:
        version = self.version
        for project in self._projects:
            for pin in project.pins:
                if pin != version:
                    raise SystemExit(f"{project.path}: boba-* pinned to {pin}, expected {version}")

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
        raise SystemExit("usage: version.py set <packages root> X.Y.Z")

    print(repository.set(argv[3]))
    print(Repository(Path(argv[2])).check())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
