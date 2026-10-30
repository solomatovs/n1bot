"""Сборочные декларации плагинов из pyproject пакетов: секция [tool.boba.sandbox].

Исполняется python'ом образа сборки внутри docker build (стадии site в
base.Dockerfile и decl в rootfs.Dockerfile): make TOML не разбирает. Набор
плагинов — группа программы или плагины одного rootfs; ответ — объединение
деклараций замыканий их пакетов, по строке.

Вызов: plugin_rootfs.py <команда> <корень packages> [id плагина ...]
Команды:
names  — строки установки дерева: пакеты плагинов, поставщики потребляемых
         групп entry points и payload каждого пакета замыкания.
libs   — системные библиотеки, которые грузят python-пакеты замыкания: ставятся
         в образ компиляции (Nuitka кладёт их копии в программу) и в корень с
         python для тестов.
apt    — системные пакеты, нужные в корне песочницы во время работы.
data   — точки монтирования каталогов данных в корне.
root   — каталоги-оверлеи корня.
setup  — setup-скрипты корня.
python — пакеты, которым в корне нужен python с их payload (python = true).
Без id ответ пуст.

Ошибки: SystemExit с текстом — неизвестная команда или id, которого не
объявляет ни один пакет репозитория.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Group(StrEnum):
    """Имена групп и ключей, которые читает скрипт."""

    TOOLS = "boba.tools"
    PAYLOAD = "payload"


class Key(StrEnum):
    """Ключи секции [tool.boba.sandbox]."""

    GUEST = "guest"
    ENTRY_POINTS = "entry_points"
    LIBS = "libs"
    APT = "apt"
    DATA = "data"
    ROOT = "root"
    SETUP = "setup"
    PYTHON = "python"


@dataclass(frozen=True)
class Project:
    """Пакет репозитория: зависимости, extras, декларации и entry points."""

    name: str
    directory: Path
    deps: tuple[str, ...]
    optional: dict[str, list[str]] = field(default_factory=dict)
    sandbox: dict[str, object] = field(default_factory=dict)
    entry_points: dict[str, dict[str, str]] = field(default_factory=dict)

    def specs(self, extras: frozenset[str]) -> Iterator[str]:
        yield from self.deps
        for extra in sorted(extras):
            yield from self.optional.get(extra, [])

    def strings(self, key: Key) -> list[str]:
        value = self.sandbox.get(key.value, [])
        if not isinstance(value, list):
            return []

        found: list[str] = []
        for item in value:
            found.append(str(item))

        return found

    def path(self, key: Key) -> str:
        value = self.sandbox.get(key.value, "")
        if not isinstance(value, str) or not value:
            return ""

        return str(self.directory / value)


class Projects:
    """Пакеты репозитория по их pyproject.toml."""

    SKIP = ("/build/", "/.venv/", "/node_modules/")

    def __init__(self, root: Path) -> None:
        self._by_name: dict[str, Project] = {}
        for pyproject in sorted(root.rglob("pyproject.toml")):
            if any(part in str(pyproject) for part in self.SKIP):
                continue

            project = self._read(pyproject)
            if project is None:
                continue

            self._by_name[project.name] = project

    @staticmethod
    def _read(pyproject: Path) -> Project | None:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)

        project = data.get("project", {})
        name = project.get("name")
        if not name:
            return None

        return Project(
            name=name,
            directory=pyproject.parent,
            deps=tuple(project.get("dependencies", [])),
            optional=dict(project.get("optional-dependencies", {})),
            sandbox=dict(data.get("tool", {}).get("boba", {}).get("sandbox", {})),
            entry_points=dict(project.get("entry-points", {})),
        )

    def get(self, name: str) -> Project | None:
        return self._by_name.get(name)

    def of_plugin(self, plugin_id: str) -> Project:
        for project in self._by_name.values():
            if plugin_id in project.entry_points.get(Group.TOOLS.value, {}):
                return project

        msg = f"plugin_rootfs.py: no package declares plugin {plugin_id!r}"
        raise SystemExit(msg)

    def with_group(self, group: str) -> Iterator[Project]:
        for project in self._by_name.values():
            if group in project.entry_points:
                yield project

    def guests(self) -> Iterator[Project]:
        """Пакеты, чей код исполняется в песочнице сам: guest = true."""
        for project in self._by_name.values():
            if project.sandbox.get(Key.GUEST.value) is True:
                yield project


class Spec:
    """Строка зависимости: имя и extras."""

    NAME = re.compile(r"^([A-Za-z0-9_.-]+)")
    EXTRAS = re.compile(r"\[([^\]]+)\]")

    def name_of(self, spec: str) -> str:
        found = self.NAME.match(spec.strip())
        if found is None:
            return ""

        return found.group(1)

    def extras_of(self, spec: str) -> frozenset[str]:
        found = self.EXTRAS.search(spec)
        if found is None:
            return frozenset()

        parts: list[str] = []
        for part in found.group(1).split(","):
            parts.append(part.strip())

        return frozenset(parts)


class Plugins:
    """Декларации набора плагинов: объединение по замыканиям их пакетов."""

    def __init__(self, projects: Projects, plugin_ids: Sequence[str]) -> None:
        self._projects = projects
        self._spec = Spec()

        packages: list[Project] = []
        for plugin_id in plugin_ids:
            project = projects.of_plugin(plugin_id)
            if project not in packages:
                packages.append(project)

        self._packages = tuple(packages)
        self._closure = tuple(self._walk())

    def _roots(self) -> Iterator[Project]:
        """Пакеты плагинов, гостевые рантаймы и поставщики потребляемых групп."""
        if not self._packages:
            return

        yield from self._packages
        yield from self._projects.guests()
        for package in self._packages:
            for group in package.strings(Key.ENTRY_POINTS):
                yield from self._projects.with_group(group)

    def _walk(self) -> Iterator[Project]:
        seen: set[tuple[str, frozenset[str]]] = set()
        found: list[str] = []
        payload = frozenset({Group.PAYLOAD.value})
        queue: list[tuple[str, frozenset[str]]] = []
        for root in self._roots():
            queue.append((root.name, payload))

        while queue:
            name, wanted = queue.pop()
            if (name, wanted) in seen:
                continue

            seen.add((name, wanted))
            project = self._projects.get(name)
            if project is None:
                continue

            if name not in found:
                found.append(name)

            for spec in project.specs(wanted | payload):
                queue.append((self._spec.name_of(spec), self._spec.extras_of(spec)))

        for name in sorted(found):
            project = self._projects.get(name)
            if project is not None:
                yield project

    def names(self) -> Iterator[str]:
        """Пакеты-корни явно, payload каждого пакета замыкания отдельной
        строкой: extras транзитивно не активируются."""
        roots: list[str] = []
        for root in self._roots():
            roots.append(root.name)

        for project in self._closure:
            if Group.PAYLOAD.value in project.optional:
                yield f"{project.name}[{Group.PAYLOAD.value}]"
                continue

            if project.name in roots:
                yield project.name

    def strings(self, key: Key) -> Iterator[str]:
        seen: list[str] = []
        for project in self._closure:
            for value in project.strings(key):
                if value in seen:
                    continue

                seen.append(value)
                yield value

    def paths(self, key: Key) -> Iterator[str]:
        for project in self._closure:
            path = project.path(key)
            if path:
                yield path

    def python(self) -> Iterator[str]:
        for project in self._packages:
            if project.sandbox.get(Key.PYTHON.value) is True:
                yield project.name


class Command(StrEnum):
    """Команды скрипта."""

    NAMES = "names"
    LIBS = "libs"
    APT = "apt"
    DATA = "data"
    ROOT = "root"
    SETUP = "setup"
    PYTHON = "python"

    def lines(self, plugins: Plugins) -> Iterator[str]:
        if self is Command.NAMES:
            return plugins.names()

        if self is Command.PYTHON:
            return plugins.python()

        if self is Command.ROOT:
            return plugins.paths(Key.ROOT)

        if self is Command.SETUP:
            return plugins.paths(Key.SETUP)

        return plugins.strings(Key(self.value))


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    try:
        command = Command(argv[1])
    except ValueError:
        print(f"plugin_rootfs.py: unknown command {argv[1]!r}", file=sys.stderr)
        return 2

    plugins = Plugins(Projects(Path(argv[2])), argv[3:])
    for line in command.lines(plugins):
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
