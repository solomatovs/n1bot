"""Tool workflow: соединение инструментов реестра стадий в граф.

Ошибки: WorkflowError разбора спеки и ошибки порта запуска (LauncherError,
PayloadFailureError) уходят наверх — их показывает ToolErrorGuard.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import Field

from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.result import JsonResult, ToolResult, pack_result
from boba.toolkit.workflow import WorkflowOutcome, WorkflowSpec

__all__ = ["build_workflow_tool"]


def build_workflow_tool(launcher: ToolLauncher, stages: Sequence[str]) -> BaseTool:
    """Фасад графа стадий: спека nodes/edges от LLM, исполнение — порт запуска.

    stages — имена узлов реестра: без них модель называет узлом имя, которого
    в реестре нет, и получает отказ на валидации.
    """
    known = ", ".join(sorted(stages))

    @tool(response_format="content_and_artifact")
    def workflow(
        nodes: Annotated[
            list[dict[str, Any]],
            Field(
                min_length=1,
                description=(
                    "Узлы графа: {id, tool, args, stdin?}. id — имя узла в "
                    "графе, tool — узловой инструмент, args — его аргументы, "
                    "stdin — необязательная строка на вход узла. "
                    f"Узловые инструменты: {known}. Имена обычных инструментов "
                    "здесь не работают."
                ),
            ),
        ],
        edges: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "Рёбра {src, dst}: вывод src подаётся на вход dst. "
                    "У узла не более одного входа; ветвление — только "
                    "исходящее. Пустой список — независимые узлы."
                ),
            ),
        ],
    ) -> tuple[str, ToolResult]:
        """Соединяет инструменты в граф: вывод одного подаётся на вход другому,
        как `a | b` в shell. Все узлы работают одновременно, промежуточные
        данные текут напрямую и в чат не попадают. Один узел может кормить
        несколько."""
        spec = WorkflowSpec.parse({"nodes": nodes, "edges": edges})

        outcome = launcher.call(spec)

        return pack_result(JsonResult(ok=True, payload=_payload_of(outcome)))

    return workflow


def _payload_of(outcome: WorkflowOutcome) -> dict[str, Any]:
    """Квитанции всех стадий — источник ответа для LLM (часть IV, п. 6)."""
    stages: list[dict[str, Any]] = []
    for stage in outcome.stages:
        stages.append(stage.model_dump())

    return {"stages": stages, "trailers": dict(outcome.trailers)}
