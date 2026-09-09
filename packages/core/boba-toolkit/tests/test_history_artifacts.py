"""Сохранённые артефакты переживают версии результата.

Чекпойнтер хранит результаты инструментов дольше кода: снимок каждого вида
в tests/history — то, что лежало в истории на момент записи, включая поля
прежних версий (`diagnostic` у shell). Снимки не перезаписываются под
модель: новое поле — новый снимок рядом, а не правка старого.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boba.toolkit.result import ResultKinds, ToolArtifact

HISTORY = Path(__file__).parent / "history"


def _snapshots() -> list[Path]:
    return sorted(HISTORY.glob("*.json"))


class TestHistoryArtifacts:
    def test_every_kind_has_a_snapshot(self) -> None:
        """Новый вид результата обязан оставить снимок: иначе его историю
        никто не проверяет."""
        covered: set[str] = set()
        for path in _snapshots():
            covered.add(json.loads(path.read_text(encoding="utf-8"))["kind"])

        missing = sorted(ResultKinds.kinds() - covered)
        if missing:
            raise AssertionError(f"kinds without a history snapshot: {missing}")

    @pytest.mark.parametrize("path", _snapshots(), ids=lambda path: path.stem)
    def test_snapshot_revives_and_renders(self, path: Path) -> None:
        stored = json.loads(path.read_text(encoding="utf-8"))

        revived = ToolArtifact.revive(stored)
        if revived is None:
            raise AssertionError(f"{path.name}: kind {stored['kind']!r} is unknown")

        if revived.kind != stored["kind"]:
            raise AssertionError(f"{path.name}: revived as {revived.kind}")

        if not revived.llm_view():
            raise AssertionError(f"{path.name}: empty llm view")

        revived.chat_view()
        revived.studio_view()
