"""Сохранённые артефакты переживают версии результата.

Чекпойнтер хранит результаты инструментов дольше кода: снимок каждого вида
в tests/history — то, что лежало в истории на момент записи, включая поля
прежних версий (`diagnostic` у shell). Снимки не перезаписываются под
модель: новое поле — новый снимок рядом, а не правка старого.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.stand_core.history import HistorySnapshots
from boba.toolkit.chain import GroupFailureResult
from boba.toolkit.dag import WorkflowResult

SNAPSHOTS = HistorySnapshots(Path(__file__).parent / "history", "boba.toolkit")


class TestHistoryArtifacts:
    def test_every_kind_has_a_snapshot(self) -> None:
        """Новый вид результата обязан оставить снимок: иначе его историю
        никто не проверяет."""
        if GroupFailureResult.declared_kind() is None:
            raise AssertionError("вид срыва группы объявлен в boba.toolkit.chain")

        missing = SNAPSHOTS.missing()
        if missing:
            raise AssertionError(f"kinds without a history snapshot: {missing}")

    @pytest.mark.parametrize("path", SNAPSHOTS.paths(), ids=lambda path: path.stem)
    def test_snapshot_revives_and_renders(self, path: Path) -> None:
        SNAPSHOTS.check(path)
