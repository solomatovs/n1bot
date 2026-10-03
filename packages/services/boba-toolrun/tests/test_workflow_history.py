"""Снимки истории видов результата, которые производит toolrun (workflow)."""

from __future__ import annotations

from pathlib import Path

import pytest

from boba.stand_core.history import HistorySnapshots
from boba.toolrun.stream_calls import WorkflowResult

SNAPSHOTS = HistorySnapshots(Path(__file__).parent / "history", "boba.toolrun")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Проверка читает снимки: сессия приложения этому тесту не нужна."""


class TestHistoryArtifacts:
    def test_every_kind_has_a_snapshot(self) -> None:
        if WorkflowResult.declared_kind() is None:
            raise AssertionError("вид workflow объявлен в boba.toolrun.stream_calls")

        missing = SNAPSHOTS.missing()
        if missing:
            raise AssertionError(f"kinds without a history snapshot: {missing}")

    @pytest.mark.parametrize("path", SNAPSHOTS.paths(), ids=lambda path: path.stem)
    def test_snapshot_revives_and_renders(self, path: Path) -> None:
        SNAPSHOTS.check(path)
