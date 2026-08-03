"""Проверка figure-спеки в песочнице."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from boba.chainlit2.agent.tools.chart.config import ChartToolsConfig
from boba.chainlit2.agent.tools.chart.protocol import (
    ValidateFigureAnswer,
    ValidateFigureRequest,
)
from boba.chainlit2.sandbox import SandboxCaller

__all__ = ["ChartCaller"]


class ChartCaller:
    """Один вызов payload'а на график; спека едет в запросе."""

    def __init__(
        self,
        tool: str,
        cfg: ChartToolsConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._entry = cfg.sandbox.entry
        self._caller = SandboxCaller(tool, cfg.sandbox.effective(), path_vars)

    def validate(self, spec: str) -> str:
        """Заголовок графика; спека, не прошедшая схему, роняет payload."""
        request = ValidateFigureRequest.of(spec)
        answer = self._caller.call_json(self._entry, request, ValidateFigureAnswer)
        return answer.title
