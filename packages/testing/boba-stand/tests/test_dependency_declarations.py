"""Зависимости каждого пакета репозитория объявлены ровно по его импортам."""

from __future__ import annotations

from pathlib import Path

from boba.stand.deps import DepsAudit


class TestDependencyDeclarations:
    def test_pyproject_matches_imports(self) -> None:
        packages = Path(__file__).resolve().parents[3]

        audit = DepsAudit(packages)
        findings = audit.findings()

        assert not findings, "\n" + audit.render(findings)
