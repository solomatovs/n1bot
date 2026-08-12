"""Узел bash для стенда графов: вклад в реестр и заготовки команд сценариев.

Ошибки: pydantic.ValidationError — args узла не по контракту.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from boba.stand.flow import StageContribution, StandPaths, StandSandbox
from boba.tool.shell.protocol import BashStage
from boba.toolkit.workflow import StageSpec


class BashNodes:
    """Узел bash: универсальный адаптер сценариев, сеть ему не нужна."""

    PACKAGES: ClassVar[tuple[str, ...]] = (
        "core/boba-cancellation",
        "core/boba-toolkit",
        "tools/boba-tool-shell",
    )

    @classmethod
    def contribution(cls, workspace: Path) -> StageContribution:
        """Узел bash с профилем стенда: изоляция от сети, общий /workspace."""
        sandbox = StandSandbox(
            packages=cls.PACKAGES,
            rw_binds=(StandPaths.workspace_bind(workspace),),
            network=False,
        )

        return StageContribution(nodes=BashStage.stages(), profile=sandbox.profile())

    @staticmethod
    def run(stage_id: str, command: str) -> StageSpec:
        return StageSpec(id=stage_id, tool=BashStage.NAME, args={"command": command})

    @staticmethod
    def literal(stage_id: str, command: str, stdin: str) -> StageSpec:
        """Узел со своим входом-литералом: ребра у него нет, вход задан спекой."""
        return StageSpec(
            id=stage_id,
            tool=BashStage.NAME,
            args={"command": command},
            stdin=stdin,
        )
