"""Зависимости каждого пакета репозитория объявлены ровно по его импортам."""

from __future__ import annotations

from pathlib import Path

from boba.stand_core.deps import DepsAudit, Layer


class TestDependencyDeclarations:
    def test_pyproject_matches_imports(self) -> None:
        packages = Path(__file__).resolve().parents[3]

        audit = DepsAudit(packages)
        findings = audit.findings()

        assert not findings, "\n" + audit.render(findings)


class TestStandCoreStaysInCore:
    """boba-stand-core — ядро стенда для core-пакетов: ни одной зависимости выше
    core и никакой инфраструктуры, только домен, pydantic и pytest."""

    THIRD_PARTY_ALLOWED = frozenset({"pydantic", "pytest"})

    def test_dependencies_are_core_only(self) -> None:
        packages = Path(__file__).resolve().parents[3]
        audit = DepsAudit(packages)
        projects = {project.name: project for project in audit.projects()}
        stand_core = projects["boba-stand-core"]

        outside: list[str] = []
        for requirement in stand_core.dependencies:
            if requirement.name in self.THIRD_PARTY_ALLOWED:
                continue
            if requirement.name not in projects:
                outside.append(requirement.name)
                continue
            if audit.layer_of(requirement.name) is not Layer.CORE:
                outside.append(
                    f"{requirement.name} ({audit.layer_of(requirement.name)})"
                )

        assert not outside, f"boba-stand-core depends outside core: {outside}"
        assert audit.layer_of("boba-stand-core") is Layer.CORE
