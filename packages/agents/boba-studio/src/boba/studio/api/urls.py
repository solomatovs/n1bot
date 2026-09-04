"""Адреса API: версия и пути ресурсов относительно точки монтирования."""

from __future__ import annotations

from enum import StrEnum

from boba.connection_broker.api import ConnectionUrl

__all__ = [
    "AccountUrl",
    "ApiVersion",
    "ConnectionUrl",
    "SignInUrl",
    "ToolCallUrl",
    "WorkflowUrl",
]


class ApiVersion(StrEnum):
    """Сегмент версии перед путями ресурсов."""

    V1 = "/v1"


class SignInUrl(StrEnum):
    """Вход по паролю, SPNEGO-обмен, обновление сессии, выход."""

    PROVIDERS = "/auth/providers"
    LOGIN = "/auth/login"
    LOGOUT = "/auth/logout"
    SSO = "/auth/sso"
    REFRESH = "/auth/refresh"


class AccountUrl(StrEnum):
    """Кто вошёл и какие профили ему видны."""

    ME = "/me"
    PROFILE = "/me/profile"
    PROFILES = "/profiles"


class ToolCallUrl(StrEnum):
    """Каталог инструментов субъекта и REST-запуск одного: имя в пути."""

    CATALOG = "/tools"
    CALL = "/tools/{name}"


class WorkflowUrl(StrEnum):
    """REST workflow: определения и запуски; профиль — в теле или query."""

    VALIDATE = "/workflows/validate"
    WORKFLOWS = "/workflows"
    WORKFLOW = "/workflows/{workflow_id}"
    WORKFLOW_DRAFT = "/workflows/{workflow_id}/draft"
    RUN = "/workflows/{workflow_id}/run"
    RUNS = "/workflow-runs"
    RUN_ONE = "/workflow-runs/{run_id}"
    STOP = "/workflow-runs/{run_id}/stop"
    STREAM = "/workflow-runs/{run_id}/streams/{call_id}"
    STREAM_CHANNELS = "/workflow-runs/{run_id}/streams/{call_id}/channels"
