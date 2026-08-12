"""Проверка figure-спеки в песочнице: узел visualize и его вызов фасадом.

Узел потоков не имеет — заголовок графика приезжает данными квитанции.

Ошибки: PayloadFailureError — спека не прошла схему plotly; LauncherError и
WorkflowError — запуск сорвался либо итог не по контракту.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from boba.tool.chart.protocol import (
    ChartStage,
    ValidateFigureAnswer,
    ValidateFigureRequest,
    ValidateFigureSettings,
)
from boba.toolkit.launcher import LauncherFactory, StageRun
from boba.toolkit.workflow import (
    StageArgsEnricher,
    StageContract,
    StageNode,
)

__all__ = ["ChartCaller"]


class ChartCaller:
    """Один вызов payload'а на график: вырожденный граф из одного узла."""

    ENTRY: ClassVar[tuple[str, ...]] = ("python3", "-m", "boba.tool.chart.payload")

    SPEC_ARG: ClassVar[str] = "spec"

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    @classmethod
    def stages(cls) -> Mapping[str, StageNode]:
        """Узлы пакета для реестра стадий; профиль подставляет приложение."""
        contract = StageContract(
            out=None,
            result=ValidateFigureAnswer,
        )
        settings = ValidateFigureSettings(op=ValidateFigureRequest.OP)

        node = StageNode(
            contract=contract,
            entry=cls.ENTRY,
            request=ValidateFigureRequest,
            enrich=StageArgsEnricher(settings),
        )

        return {ChartStage.VISUALIZE: node}

    def validate(self, spec: str) -> str:
        """Заголовок графика; спека, не прошедшая схему, роняет стадию."""
        answer = self._run.trailer(
            ChartStage.VISUALIZE,
            {self.SPEC_ARG: spec},
            ValidateFigureAnswer,
        )

        return answer.title
