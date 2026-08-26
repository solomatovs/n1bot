"""Адреса API: версия и пути ресурсов относительно точки монтирования."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ApiVersion", "ToolCallUrl", "WorkflowUrl"]


class ApiVersion(StrEnum):
    """Сегмент версии перед путями ресурсов."""

    V1 = "/v1"


class ToolCallUrl(StrEnum):
    """REST-запуск инструмента: имя в пути, остальное в теле."""

    CALL = "/tools/{name}"


class WorkflowUrl(StrEnum):
    """REST workflow: определения и запуски; профиль — в теле или query."""

    CATALOG = "/workflows/catalog"
    VALIDATE = "/workflows/validate"
    WORKFLOWS = "/workflows"
    WORKFLOW = "/workflows/{workflow_id}"
    RUN = "/workflows/{workflow_id}/run"
    RUNS = "/workflow-runs"
    RUN_ONE = "/workflow-runs/{run_id}"
    STOP = "/workflow-runs/{run_id}/stop"
