"""Зависимости каждого пакета репозитория объявлены ровно по его импортам."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.stand_core.deps import DepsAudit, ImportScan, Layer


class TestDependencyDeclarations:
    def test_pyproject_matches_imports(self) -> None:
        packages = Path(__file__).resolve().parents[3]

        audit = DepsAudit(packages)
        findings = audit.findings()

        assert not findings, "\n" + audit.render(findings)


class TestStandCoreStaysInCore:
    """boba-stand-core — ядро стенда для core-пакетов: ни одной зависимости выше
    core и никакой инфраструктуры: домен, DTO, pytest и разбор metadata пакетов."""

    THIRD_PARTY_ALLOWED = frozenset({"packaging", "pydantic", "pytest"})

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


class TestAccessStaysPure:
    def test_policy_does_not_pull_storage_or_tool_execution(self) -> None:
        packages = Path(__file__).resolve().parents[3]
        audit = DepsAudit(packages)
        projects = {project.name: project for project in audit.projects()}
        access = projects["boba-access"]

        allowed = {"boba-core", "pydantic"}
        outside = [
            requirement.name
            for requirement in access.dependencies
            if requirement.name not in allowed
        ]

        assert not outside, f"boba-access pulls infrastructure: {outside}"


class TestConnectionModelsStayInCore:
    def test_models_do_not_pull_crypto_or_services(self) -> None:
        packages = Path(__file__).resolve().parents[3]
        audit = DepsAudit(packages)
        projects = {project.name: project for project in audit.projects()}
        connections = projects["boba-connections"]

        outside: list[str] = []
        for requirement in connections.dependencies:
            if requirement.name == "pydantic":
                continue

            if requirement.name not in projects:
                outside.append(requirement.name)
                continue

            if audit.layer_of(requirement.name) is not Layer.CORE:
                outside.append(requirement.name)

        assert not outside, f"boba-connections pulls infrastructure: {outside}"


class TestToolContractsAndIoBoundaries:
    @pytest.mark.parametrize("name", ["boba-toolkit", "boba-tool-io"])
    def test_dependencies_do_not_pull_host_services(self, name: str) -> None:
        packages = Path(__file__).resolve().parents[3]
        audit = DepsAudit(packages)
        projects = {project.name: project for project in audit.projects()}
        project = projects[name]

        outside: list[str] = []
        for requirement in project.dependencies:
            if requirement.name not in projects:
                continue

            if audit.layer_of(requirement.name) is Layer.CORE:
                continue

            outside.append(requirement.name)

        assert not outside, f"{name} pulls host infrastructure: {outside}"

    def test_contract_modules_do_not_import_native_io(self) -> None:
        packages = Path(__file__).resolve().parents[3]
        source = packages / "core/boba-toolkit/src/boba/toolkit"
        native = {"os", "fcntl", "termios", "ctypes", "subprocess", "selectors"}
        scan = ImportScan()

        leaked: list[str] = []
        for path, names in scan.modules_by_file(source):
            for name in names:
                if name.split(".")[0] in native:
                    leaked.append(f"{path.name}: {name}")

        assert not leaked, f"toolkit contracts import native I/O: {leaked}"
