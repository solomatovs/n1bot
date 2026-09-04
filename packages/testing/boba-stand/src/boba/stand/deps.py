"""Сверка объявленных зависимостей пакетов репозитория с их импортами.

Источник правды — pyproject каждого пакета и AST его модулей: src сверяется с
dependencies и extras, tests — с dependencies и экстрой dev. Владельца стороннего
модуля даёт список файлов дистрибутивов текущего интерпретатора, владельца
boba.* — каталоги src пакетов репозитория.

Ошибки:
DepsAuditError — pyproject не разбирается, каталог packages не найден или модуль
    не принадлежит ни одному дистрибутиву.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

__all__ = ["DepsAudit", "DepsAuditError", "Finding", "PackageProject"]


class DepsAuditError(Exception):
    """Разбор пакетов или владельцев модулей невозможен."""


class BundleExtra(StrEnum):
    """Экстры, чьи члены ставятся ради гостя или образа, а не импортируются пакетом."""

    PAYLOAD = "payload"
    TOOLS = "tools"
    GENERATION = "generation"

    @classmethod
    def covers(cls, extra: str) -> bool:
        values = [member.value for member in cls]

        return extra in values


class FileSuffix(StrEnum):
    """Суффиксы файлов дистрибутива, по которым восстанавливается имя модуля."""

    PY = ".py"
    SO = ".so"
    PYD = ".pyd"
    DIST_INFO = ".dist-info"
    EGG_INFO = ".egg-info"

    @classmethod
    def binary(cls) -> tuple[str, ...]:
        return (cls.SO.value, cls.PYD.value)

    @classmethod
    def info(cls) -> tuple[str, ...]:
        return (cls.DIST_INFO.value, cls.EGG_INFO.value)


class Scope(StrEnum):
    """Какая часть пакета сверяется."""

    SRC = "src"
    TESTS = "tests"


class FindingKind(StrEnum):
    """Расхождение между pyproject и импортами."""

    MISSING = "missing"
    UNUSED = "unused"


class Requirement(BaseModel):
    """Строка зависимости: нормализованное имя и extras."""

    model_config = ConfigDict(frozen=True)

    NAME: ClassVar[re.Pattern[str]] = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
    EXTRAS: ClassVar[re.Pattern[str]] = re.compile(
        r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*\[([^\]]*)\]"
    )
    EXTRA_MARKER: ClassVar[str] = 'extra == "{extra}"'

    name: str
    extras: frozenset[str] = frozenset()

    @classmethod
    def normalize(cls, name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    @classmethod
    def parse(cls, spec: str) -> Requirement:
        found = cls.NAME.match(spec)
        if found is None:
            msg = (
                f"requirement {spec!r} does not match a PEP 508 name: "
                f"{cls.NAME.pattern}"
            )
            raise DepsAuditError(msg)

        extras: set[str] = set()
        with_extras = cls.EXTRAS.match(spec)
        if with_extras is not None:
            for part in with_extras.group(1).split(","):
                extras.add(part.strip())

        return cls(name=cls.normalize(found.group(1)), extras=frozenset(extras))

    def marker_of(self, extra: str) -> str:
        return self.EXTRA_MARKER.format(extra=extra)


class PackageProject(BaseModel):
    """Пакет репозитория: имя, каталог и объявленные зависимости."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    FILE: ClassVar[str] = "pyproject.toml"
    DEV: ClassVar[str] = "dev"
    SRC_DIR: ClassVar[str] = "src"
    TESTS_DIR: ClassVar[str] = "tests"
    NAMESPACE: ClassVar[str] = "boba"

    name: str
    root: Path
    dependencies: Sequence[Requirement]
    extras: Mapping[str, Sequence[Requirement]]

    @classmethod
    def load(cls, pyproject: Path) -> PackageProject:
        try:
            with pyproject.open("rb") as handle:
                project = tomllib.load(handle)["project"]
        except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
            msg = f"reading [project] of {pyproject}: {type(exc).__name__}: {exc}"
            raise DepsAuditError(msg) from exc

        dependencies: list[Requirement] = []
        for spec in project.get("dependencies", []):
            dependencies.append(Requirement.parse(spec))

        extras: dict[str, list[Requirement]] = {}
        for extra, specs in project.get("optional-dependencies", {}).items():
            extras[extra] = []
            for spec in specs:
                extras[extra].append(Requirement.parse(spec))

        return cls(
            name=Requirement.normalize(project["name"]),
            root=pyproject.parent,
            dependencies=dependencies,
            extras=extras,
        )

    @property
    def src(self) -> Path:
        return self.root / self.SRC_DIR

    @property
    def tests(self) -> Path:
        return self.root / self.TESTS_DIR

    @property
    def namespace_dir(self) -> Path:
        return self.src / self.NAMESPACE

    def has_tests(self) -> bool:
        if not self.tests.is_dir():
            return False

        return any(self.tests.rglob("*.py"))

    def dev_requirements(self) -> Sequence[Requirement]:
        return self.extras.get(self.DEV, ())

    def all_requirements(self) -> Iterator[Requirement]:
        yield from self.dependencies

        for requirements in self.extras.values():
            yield from requirements


class ModuleOwners:
    """Владелец модуля: дистрибутив в интерпретаторе или пакет репозитория."""

    INIT: ClassVar[str] = "__init__.py"
    PYTEST_PLUGIN_GROUP: ClassVar[str] = "pytest11"
    PYTEST: ClassVar[str] = "pytest"

    def __init__(self, projects: Sequence[PackageProject]) -> None:
        self._stdlib = set(sys.stdlib_module_names)
        self._third: dict[str, str] = {}
        self._plugins: set[str] = set()
        self._local: dict[str, str] = {}
        self._projects = {project.name: project for project in projects}

        for dist in metadata.distributions():
            self._index_distribution(dist)

        for project in projects:
            if not project.namespace_dir.is_dir():
                continue
            self._index_tree(
                project.namespace_dir, PackageProject.NAMESPACE, project.name
            )

    def _index_distribution(self, dist: metadata.Distribution) -> None:
        raw_name = dist.metadata["Name"]
        if raw_name is None:
            return

        name = Requirement.normalize(raw_name)
        if name.startswith(f"{PackageProject.NAMESPACE}-"):
            return

        for entry in dist.entry_points:
            if entry.group == self.PYTEST_PLUGIN_GROUP:
                self._plugins.add(name)

        for path in dist.files or ():
            module = self._module_of(path)
            if module is None:
                continue
            self._third.setdefault(module, name)

    def _module_of(self, path: metadata.PackagePath) -> str | None:
        parts = list(path.parts)
        if not parts:
            return None

        leaf = parts[-1]
        if leaf.endswith(FileSuffix.PY):
            parts = parts[:-1]
            if leaf != self.INIT:
                parts.append(leaf.removesuffix(FileSuffix.PY))
        elif leaf.endswith(FileSuffix.binary()):
            parts = [*parts[:-1], leaf.split(".")[0]]
        else:
            return None

        if not parts:
            return None

        if parts[0] in ("..", "bin", "lib"):
            return None

        for part in parts:
            if part.endswith(FileSuffix.info()):
                return None

        return ".".join(parts)

    def _index_tree(self, directory: Path, prefix: str, owner: str) -> None:
        for child in sorted(directory.iterdir()):
            if child.name.startswith("__") or child.name.startswith("."):
                continue

            key = f"{prefix}.{child.name.removesuffix('.py')}"

            is_namespace = child.is_dir() and not (child / self.INIT).exists()
            if is_namespace:
                self._index_tree(child, key, owner)
                continue

            previous = self._local.get(key)
            if previous is not None and previous != owner:
                msg = (
                    f"indexing {child}: module {key} is owned by both "
                    f"{previous} and {owner}"
                )
                raise DepsAuditError(msg)

            self._local[key] = owner

    def is_plugin_of_pytest(self, dist: str) -> bool:
        return dist in self._plugins

    def dists_of(
        self, modules: Iterable[str], local_roots: Sequence[Path]
    ) -> Iterator[str]:
        """Дистрибутивы импортируемых модулей; stdlib и локальное пропускаются."""
        for module in modules:
            top = module.split(".")[0]

            if top in self._stdlib:
                continue

            if top == PackageProject.NAMESPACE:
                yield self._boba_owner(module)
                continue

            if self._is_local_module(top, local_roots):
                continue

            yield self._third_owner(module)

    def _is_local_module(self, top: str, local_roots: Sequence[Path]) -> bool:
        for root in local_roots:
            if (root / f"{top}.py").is_file():
                return True
            if (root / top).is_dir():
                return True

        return False

    def _boba_owner(self, module: str) -> str:
        parts = module.split(".")
        while len(parts) > 1:
            owner = self._local.get(".".join(parts))
            if owner is not None:
                return owner
            parts.pop()

        known = len(self._local)
        msg = f"module {module}: none of the {known} indexed repository modules owns it"
        raise DepsAuditError(msg)

    def _third_owner(self, module: str) -> str:
        parts = module.split(".")
        while parts:
            owner = self._third.get(".".join(parts))
            if owner is not None:
                return owner
            parts.pop()

        msg = f"module {module}: no installed distribution owns it"
        raise DepsAuditError(msg)

    def brought_by_extras(self, requirement: Requirement) -> set[str]:
        """Дистрибутивы extras-части зависимости: psycopg[pool] → psycopg-pool."""
        if not requirement.extras:
            return set()

        project = self._projects.get(requirement.name)
        if project is not None:
            return self._workspace_extra_members(project, requirement.extras)

        return self._distribution_extra_members(requirement)

    def _workspace_extra_members(
        self, project: PackageProject, extras: frozenset[str]
    ) -> set[str]:
        members: set[str] = set()
        for extra in extras:
            for member in project.extras.get(extra, ()):
                members.add(member.name)

        return members

    def _distribution_extra_members(self, requirement: Requirement) -> set[str]:
        try:
            requires = metadata.requires(requirement.name)
        except metadata.PackageNotFoundError:
            return set()

        if requires is None:
            return set()

        members: set[str] = set()
        for line in requires:
            for extra in requirement.extras:
                if requirement.marker_of(extra) not in line:
                    continue
                members.add(Requirement.parse(line).name)

        return members


class ImportScan:
    """Абсолютные импорты всех модулей каталога по AST."""

    SUFFIX: ClassVar[str] = "*.py"

    def modules_by_file(self, directory: Path) -> Iterator[tuple[Path, set[str]]]:
        for path in sorted(directory.rglob(self.SUFFIX)):
            yield path, self._modules_of(path)

    def _modules_of(self, path: Path) -> set[str]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            msg = f"parsing module {path}: {type(exc).__name__}: {exc}"
            raise DepsAuditError(msg) from exc

        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
                continue

            if not isinstance(node, ast.ImportFrom):
                continue

            if node.level:
                continue

            if node.module is None:
                continue

            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")

        return modules


class Finding(BaseModel):
    """Одно расхождение: дистрибутив, которого не хватает или который лишний."""

    model_config = ConfigDict(frozen=True)

    package: str
    scope: Scope
    kind: FindingKind
    dist: str
    files: Sequence[str] = ()

    def render(self) -> str:
        where = ", ".join(self.files[:3])
        line = f"{self.package} [{self.scope}] {self.kind} {self.dist}  {where}"

        return line.rstrip()


class DepsAudit:
    """Проход по всем пакетам репозитория: src против pyproject, tests против dev."""

    GLOBS: ClassVar[tuple[str, ...]] = ("*/*/pyproject.toml", "*/*/*/pyproject.toml")

    def __init__(self, packages_root: Path) -> None:
        if not packages_root.is_dir():
            msg = f"deps audit: packages root {packages_root} is not a directory"
            raise DepsAuditError(msg)

        self._root = packages_root
        self._projects = self._load_projects()
        self._owners = ModuleOwners(self._projects)
        self._scan = ImportScan()

    def _load_projects(self) -> list[PackageProject]:
        projects: list[PackageProject] = []
        for pattern in self.GLOBS:
            for pyproject in sorted(self._root.glob(pattern)):
                projects.append(PackageProject.load(pyproject))

        return projects

    def projects(self) -> Sequence[PackageProject]:
        return self._projects

    def findings(self) -> list[Finding]:
        result: list[Finding] = []
        for project in self._projects:
            result.extend(self._src_findings(project))
            result.extend(self._tests_findings(project))

        return result

    def render(self, findings: Sequence[Finding]) -> str:
        lines: list[str] = []
        for finding in findings:
            lines.append(finding.render())

        return "\n".join(lines)

    def _src_findings(self, project: PackageProject) -> Iterator[Finding]:
        used = self._used(project, project.src)

        declared: set[str] = set()
        via_extras: set[str] = set()
        for requirement in project.all_requirements():
            declared.add(requirement.name)
            via_extras |= self._owners.brought_by_extras(requirement)

        for dist in sorted(used):
            if dist in declared:
                continue
            if dist in via_extras:
                continue
            yield Finding(
                package=project.name,
                scope=Scope.SRC,
                kind=FindingKind.MISSING,
                dist=dist,
                files=sorted(used[dist]),
            )

        yield from self._unused(project, Scope.SRC, project.dependencies, used)

        for extra, requirements in project.extras.items():
            if BundleExtra.covers(extra):
                continue
            if extra == PackageProject.DEV:
                continue
            yield from self._unused(project, Scope.SRC, requirements, used)

    def _tests_findings(self, project: PackageProject) -> Iterator[Finding]:
        if not project.has_tests():
            return

        used = self._used(project, project.tests)

        covered: set[str] = set()
        for requirement in project.dependencies:
            covered.add(requirement.name)
            covered |= self._owners.brought_by_extras(requirement)

        for requirement in project.dev_requirements():
            covered.add(requirement.name)
            covered |= self._owners.brought_by_extras(requirement)

        for dist in sorted(used):
            if dist in covered:
                continue
            yield Finding(
                package=project.name,
                scope=Scope.TESTS,
                kind=FindingKind.MISSING,
                dist=dist,
                files=sorted(used[dist]),
            )

        yield from self._unused(project, Scope.TESTS, project.dev_requirements(), used)

    def _used(self, project: PackageProject, directory: Path) -> dict[str, set[str]]:
        used: dict[str, set[str]] = {}
        for path, modules in self._scan.modules_by_file(directory):
            local_roots = (directory, path.parent)
            for dist in self._owners.dists_of(modules, local_roots):
                if dist == project.name:
                    continue
                used.setdefault(dist, set()).add(str(path.relative_to(directory)))

        return used

    def _unused(
        self,
        project: PackageProject,
        scope: Scope,
        requirements: Sequence[Requirement],
        used: Mapping[str, set[str]],
    ) -> Iterator[Finding]:
        names: set[str] = set()
        for requirement in requirements:
            names.add(requirement.name)

        has_pytest = ModuleOwners.PYTEST in names

        for requirement in requirements:
            if requirement.name in used:
                continue

            is_plugin = self._owners.is_plugin_of_pytest(requirement.name)
            if has_pytest and is_plugin:
                continue

            yield Finding(
                package=project.name,
                scope=scope,
                kind=FindingKind.UNUSED,
                dist=requirement.name,
            )
