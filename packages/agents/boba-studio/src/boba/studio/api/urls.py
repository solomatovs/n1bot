"""Адреса API: версия и пути ресурсов относительно точки монтирования."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["AccountUrl", "ApiVersion", "ConnectionUrl", "ToolCallUrl", "WorkflowUrl"]


class ApiVersion(StrEnum):
    """Сегмент версии перед путями ресурсов."""

    V1 = "/v1"


class AccountUrl(StrEnum):
    """Кто вошёл и какие профили ему видны."""

    ME = "/me"
    PROFILES = "/profiles"


class ConnectionUrl(StrEnum):
    """Соединения пользователя: список, свои — создание, замена, удаление."""

    CONNECTIONS = "/connections"
    CONNECTION = "/connections/{connection_id}"


class ToolCallUrl(StrEnum):
    """Каталог инструментов субъекта и REST-запуск одного: имя в пути."""

    CATALOG = "/tools"
    CALL = "/tools/{name}"


class WorkflowUrl(StrEnum):
    """REST workflow: определения и запуски; профиль — в теле или query."""

    VALIDATE = "/workflows/validate"
    WORKFLOWS = "/workflows"
    WORKFLOW = "/workflows/{workflow_id}"
    RUN = "/workflows/{workflow_id}/run"
    RUNS = "/workflow-runs"
    RUN_ONE = "/workflow-runs/{run_id}"
    STOP = "/workflow-runs/{run_id}/stop"
