"""Список пакетов песочницы приложения: закрытие зависимостей boba-<app> по pyproject
репозитория, из него — пакеты с extra payload (их код и полезная нагрузка едут в
образ корня песочницы).

Вызов: sandbox_names.py <корень packages> <приложение> [extra ...]
"""

import re
import sys
import tomllib
from pathlib import Path


class Projects:
    """Пакеты репозитория: зависимости и optional-dependencies по имени."""

    SKIP = ("/build/", "/.venv/", "/node_modules/")

    def __init__(self, root: Path) -> None:
        self._deps: dict[str, list[str]] = {}
        self._optional: dict[str, dict[str, list[str]]] = {}
        for pyproject in root.rglob("pyproject.toml"):
            if any(part in str(pyproject) for part in self.SKIP):
                continue

            with pyproject.open("rb") as handle:
                project = tomllib.load(handle).get("project", {})

            name = project.get("name")
            if not name:
                continue

            self._deps[name] = list(project.get("dependencies", []))
            self._optional[name] = dict(project.get("optional-dependencies", {}))

    def known(self, name: str) -> bool:
        return name in self._deps

    def specs_of(self, name: str, extras: frozenset[str]) -> list[str]:
        specs = list(self._deps[name])
        for extra in sorted(extras):
            specs.extend(self._optional[name].get(extra, []))

        return specs

    def has_payload(self, name: str) -> bool:
        return "payload" in self._optional[name]


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


def closure(projects: Projects, app: str, extras: frozenset[str]) -> set[str]:
    seen: set[tuple[str, frozenset[str]]] = set()
    names: set[str] = set()
    queue: list[tuple[str, frozenset[str]]] = [(f"boba-{app}", extras)]
    while queue:
        name, wanted = queue.pop()
        if (name, wanted) in seen:
            continue

        seen.add((name, wanted))
        if not projects.known(name):
            continue

        names.add(name)
        for spec in projects.specs_of(name, wanted):
            queue.append((Spec.name_of(spec), Spec.extras_of(spec)))

    return names


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: sandbox_names.py <packages root> <app> [extra ...]", file=sys.stderr)
        return 2

    projects = Projects(Path(argv[1]))
    names = closure(projects, argv[2], frozenset(argv[3:]))
    for name in sorted(names):
        if projects.has_payload(name):
            print(f"{name}[payload]")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
