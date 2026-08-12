"""Фикстуры сценарных графов: живой postgres, /workspace и прогон боевых узлов."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from boba.stand.flow import FlowStand, StageContribution
from boba.stand.pg import PgNodes, PgStand
from boba.stand.shell import BashNodes


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Хостовый каталог, который стадии видят как /workspace."""
    path = tmp_path / "workspace"
    path.mkdir()

    return path


@pytest.fixture
def pg_stand() -> Iterator[PgStand]:
    """Стенд базы: пустые источник и приёмник до сценария, уборка после."""
    stand = PgStand.required()
    stand.reset()

    yield stand

    stand.drop()


@pytest.fixture
def contributions(
    pg_stand: PgStand, workspace: Path
) -> tuple[StageContribution, ...]:
    """Узлы, доступные сценарию; свой набор файл сценария задаёт перекрытием."""
    return (
        PgNodes.contribution(pg_stand.connection(), workspace),
        BashNodes.contribution(workspace),
    )


@pytest.fixture
def flow(
    contributions: tuple[StageContribution, ...], workspace: Path
) -> FlowStand:
    """Прогон графа: реестр собирается из вкладов сценария."""
    return FlowStand.of(contributions, workspace)
