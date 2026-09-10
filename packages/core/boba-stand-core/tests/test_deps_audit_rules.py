"""Правила DepsAudit на искусственном дереве пакетов: ребро против слоёв, цикл
по dependencies, модуль за extra владельца, src на dev-зависимости."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from boba.stand_core.deps import DepsAudit, FindingKind


@dataclass(frozen=True)
class FakePackage:
    """Пакет искусственного дерева: слой, имя, импорты модуля и pyproject."""

    layer: str
    name: str
    imports: Sequence[str] = ()
    dependencies: Sequence[str] = ()
    dev: Sequence[str] = ()
    extras: Mapping[str, Sequence[str]] = field(default_factory=dict)
    module_extras: Mapping[str, str] = field(default_factory=dict)

    @property
    def module(self) -> str:
        return self.name.removeprefix("boba-").replace("-", "_")

    def pyproject(self) -> str:
        lines = [
            "[project]",
            f'name = "{self.name}"',
            'version = "0"',
        ]
        lines.extend(self._array("dependencies", self.dependencies))
        lines.append("[project.optional-dependencies]")
        lines.extend(self._array("dev", self.dev))
        for extra, members in self.extras.items():
            lines.extend(self._array(extra, members))

        if self.module_extras:
            lines.append("[tool.boba.extras]")
            for gated, extra in self.module_extras.items():
                lines.append(f'"{gated}" = "{extra}"')

        return "\n".join(lines) + "\n"

    def source(self) -> str:
        lines: list[str] = []
        for imported in self.imports:
            lines.append(f"import {imported}")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _array(key: str, values: Sequence[str]) -> list[str]:
        lines = [f"{key} = ["]
        for value in values:
            lines.append(f'    "{value}",')
        lines.append("]")

        return lines


class FakeTree:
    """Дерево packages/<слой>/<пакет> с pyproject и одним модулем на пакет."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def add(self, package: FakePackage) -> None:
        pkg = self._root / package.layer / package.name
        src = pkg / "src" / "boba" / package.module
        src.mkdir(parents=True)
        (src / "__init__.py").write_text(package.source())
        (src / "gated.py").write_text("VALUE = 1\n")
        (pkg / "pyproject.toml").write_text(package.pyproject())

    def audit(self) -> DepsAudit:
        return DepsAudit(self._root)


def _kinds(audit: DepsAudit, kind: FindingKind) -> list[str]:
    return [f.dist for f in audit.findings() if f.kind is kind]


def test_core_importing_services_is_a_layer_finding(tmp_path: Path) -> None:
    tree = FakeTree(tmp_path)
    tree.add(FakePackage("services", "boba-svc"))
    tree.add(FakePackage("core", "boba-dom", ["boba.svc"], ["boba-svc"]))

    found = _kinds(tree.audit(), FindingKind.LAYER)

    assert found == ["boba-svc (core -> services)"]


def test_services_importing_tools_is_a_layer_finding(tmp_path: Path) -> None:
    tree = FakeTree(tmp_path)
    tree.add(FakePackage("tools", "boba-tool-x"))
    tree.add(FakePackage("services", "boba-svc", ["boba.tool_x"], ["boba-tool-x"]))

    found = _kinds(tree.audit(), FindingKind.LAYER)

    assert found == ["boba-tool-x (services -> tools)"]


def test_agents_importing_tools_and_testing_importing_anything_pass(
    tmp_path: Path,
) -> None:
    tree = FakeTree(tmp_path)
    tree.add(FakePackage("tools", "boba-tool-x"))
    tree.add(FakePackage("agents", "boba-app", ["boba.tool_x"], ["boba-tool-x"]))
    tree.add(FakePackage("testing", "boba-stand-x", ["boba.app"], ["boba-app"]))

    assert _kinds(tree.audit(), FindingKind.LAYER) == []


def test_dependency_cycle_is_reported_once(tmp_path: Path) -> None:
    tree = FakeTree(tmp_path)
    tree.add(FakePackage("services", "boba-a", ["boba.b"], ["boba-b"]))
    tree.add(FakePackage("services", "boba-b", ["boba.a"], ["boba-a"]))

    found = _kinds(tree.audit(), FindingKind.CYCLE)

    assert found == ["boba-a -> boba-b -> boba-a"]


def test_gated_module_requires_the_owner_extra(tmp_path: Path) -> None:
    tree = FakeTree(tmp_path)
    tree.add(
        FakePackage(
            "infra",
            "boba-krbx",
            extras={"seal": ["pydantic"]},
            module_extras={"boba.krbx.gated": "seal"},
        )
    )
    tree.add(FakePackage("services", "boba-user", ["boba.krbx.gated"], ["boba-krbx"]))
    tree.add(
        FakePackage("services", "boba-proper", ["boba.krbx.gated"], ["boba-krbx[seal]"])
    )

    audit = tree.audit()
    missing = [
        (f.package, f.dist)
        for f in audit.findings()
        if f.kind is FindingKind.MISSING_EXTRA
    ]

    assert missing == [("boba-user", "boba-krbx[seal]")]


def test_src_import_declared_only_in_dev_is_missing(tmp_path: Path) -> None:
    tree = FakeTree(tmp_path)
    tree.add(FakePackage("core", "boba-dev-only", ["pydantic"], dev=["pydantic"]))

    found = [
        (f.package, f.scope.value, f.dist)
        for f in tree.audit().findings()
        if f.kind is FindingKind.MISSING
    ]

    assert found == [("boba-dev-only", "src", "pydantic")]
