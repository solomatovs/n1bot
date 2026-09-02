"""Сборочные декларации tool-плагина из его pyproject: секция [tool.boba.sandbox].

Одна цель Makefile читает декларации и понимает, что ставить в образ корня
плагина: python-закрытие payload, apt-пакеты, оверлей корня и setup-скрипт.

Вызов: plugin_rootfs.py <команда> <корень packages> [пакет]
Команды:
names   — закрытие python-зависимостей пакета с payload (по строке).
apt     — системные пакеты декларации (по строке).
root    — каталог-оверлей корня внутри пакета; пусто — нет.
setup   — setup-скрипт внутри пакета; пусто — нет.
imports — модули smoke-проверки образа (по строке).
data    — гостевые пути данных по закрытию: точки монтирования (по строке).
list    — пакеты репозитория с entry points группы boba.tools (по строке).
"""

import re
import sys
import tomllib
from pathlib import Path

GROUP = "boba.tools"


class Projects:
    """Пакеты репозитория: зависимости, optional-dependencies и декларации."""

    SKIP = ("/build/", "/.venv/", "/node_modules/")

    def __init__(self, root: Path) -> None:
        self._deps: dict[str, list[str]] = {}
        self._optional: dict[str, dict[str, list[str]]] = {}
        self._sandbox: dict[str, dict] = {}
        self._entry_points: dict[str, dict] = {}
        self._dirs: dict[str, Path] = {}
        for pyproject in root.rglob("pyproject.toml"):
            if any(part in str(pyproject) for part in self.SKIP):
                continue

            with pyproject.open("rb") as handle:
                data = tomllib.load(handle)

            project = data.get("project", {})
            name = project.get("name")
            if not name:
                continue

            self._deps[name] = list(project.get("dependencies", []))
            self._optional[name] = dict(project.get("optional-dependencies", {}))
            self._sandbox[name] = dict(
                data.get("tool", {}).get("boba", {}).get("sandbox", {})
            )
            self._entry_points[name] = dict(project.get("entry-points", {}))
            self._dirs[name] = pyproject.parent

    def known(self, name: str) -> bool:
        return name in self._deps

    def specs_of(self, name: str, extras: frozenset[str]) -> list[str]:
        specs = list(self._deps[name])
        for extra in sorted(extras):
            specs.extend(self._optional[name].get(extra, []))

        return specs

    def sandbox_of(self, name: str) -> dict:
        return self._sandbox.get(name, {})

    def has_payload(self, name: str) -> bool:
        return "payload" in self._optional[name]

    def guests(self) -> list[str]:
        """Пакеты, чей код исполняется внутри образа корня: guest = true."""
        names: list[str] = []
        for name, sandbox in self._sandbox.items():
            if sandbox.get("guest") is True:
                names.append(name)

        return sorted(names)

    def dir_of(self, name: str) -> Path:
        return self._dirs[name]

    def with_tools(self) -> list[str]:
        names: list[str] = []
        for name, points in self._entry_points.items():
            if GROUP in points:
                names.append(name)

        return sorted(names)


class Spec:
    """Разбор строки зависимости: имя и extras."""

    NAME = re.compile(r"^([A-Za-z0-9_.-]+)")
    EXTRAS = re.compile(r"\[([^\]]+)\]")

    @classmethod
    def name_of(cls, spec: str) -> str:
        found = cls.NAME.match(spec.strip())
        if found is None:
            return ""

        return found.group(1)

    @classmethod
    def extras_of(cls, spec: str) -> frozenset[str]:
        found = cls.EXTRAS.search(spec)
        if found is None:
            return frozenset()

        return frozenset(part.strip() for part in found.group(1).split(","))


class Commands:
    """Ответы на команды CLI: декларации агрегируются по закрытию пакета."""

    def __init__(self, projects: Projects, package: str) -> None:
        if not projects.known(package):
            msg = f"package {package!r} is not in the repository"
            raise SystemExit(msg)

        self._projects = projects
        self._package = package

    def _closure(self) -> list[str]:
        """Workspace-пакеты закрытия payload-графа, включая гостевые рантаймы."""
        seen: set[tuple[str, frozenset[str]]] = set()
        names: set[str] = set()
        queue: list[tuple[str, frozenset[str]]] = [
            (self._package, frozenset({"payload"}))
        ]
        # гостевые рантаймы исполняются внутри образа и объявляют это сами
        # (guest = true в [tool.boba.sandbox]); тела от них не зависят
        for guest in self._projects.guests():
            queue.append((guest, frozenset({"payload"})))

        while queue:
            name, wanted = queue.pop()
            if (name, wanted) in seen:
                continue

            seen.add((name, wanted))
            if not self._projects.known(name):
                continue

            names.add(name)
            payload = wanted | {"payload"}
            for spec in self._projects.specs_of(name, payload):
                queue.append((Spec.name_of(spec), Spec.extras_of(spec)))

        return sorted(names)

    def names(self) -> list[str]:
        """Строки установки: каждый payload-пакет закрытия явно.

        Extras транзитивно не активируются, поэтому payload каждого пакета
        закрытия перечисляется установке отдельной строкой; остальные
        зависимости uv разрешает сам по метаданным колёс.
        """
        listed: list[str] = []
        for name in self._closure():
            if name != self._package and not self._projects.has_payload(name):
                continue

            if self._projects.has_payload(name):
                listed.append(f"{name}[payload]")
            else:
                listed.append(name)

        return listed

    def apt(self) -> list[str]:
        """Нативные пакеты всех деклараций закрытия, без повторов."""
        packages: list[str] = []
        for name in self._closure():
            for entry in self._projects.sandbox_of(name).get("apt", []):
                if entry not in packages:
                    packages.append(entry)

        return packages

    def data(self) -> list[str]:
        """Гостевые пути данных всех деклараций закрытия: их монтирует рантайм,
        а образ обязан нести пустые точки монтирования."""
        found: list[str] = []
        for name in self._closure():
            for path in self._projects.sandbox_of(name).get("data", []):
                if path not in found:
                    found.append(path)

        return found

    def imports(self) -> list[str]:
        return list(self._projects.sandbox_of(self._package).get("imports", []))

    def root(self) -> list[str]:
        """Каталоги-оверлеи корня всех деклараций закрытия."""
        found: list[str] = []
        for name in self._closure():
            relative = self._projects.sandbox_of(name).get("root", "")
            if relative:
                found.append(str(self._projects.dir_of(name) / relative))

        return found

    def setup(self) -> list[str]:
        """Setup-скрипты всех деклараций закрытия, в порядке имён пакетов."""
        found: list[str] = []
        for name in self._closure():
            relative = self._projects.sandbox_of(name).get("setup", "")
            if relative:
                found.append(str(self._projects.dir_of(name) / relative))

        return found


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    command = argv[1]
    projects = Projects(Path(argv[2]))

    if command == "list":
        lines = projects.with_tools()
    else:
        if len(argv) < 4:
            print(__doc__, file=sys.stderr)
            return 2

        commands = Commands(projects, argv[3])
        handler = getattr(commands, command, None)
        if handler is None:
            print(f"unknown command {command!r}", file=sys.stderr)
            return 2

        lines = handler()

    for line in lines:
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
