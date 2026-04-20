"""Хелперы для tools, работающих с несколькими workspace namespace'ами.

Инкапсулируют общий код:
  * построение :class:`ParamSchema` для аргумента ``workspace``,
  * парсинг и валидация значения из сырого ``dict``.

Нужны, чтобы пять tools'ов не дублировали одну и ту же логику.
"""

from __future__ import annotations

from typing import Any

from boba.domain.core.tools import (
    JsonType,
    ParamSchema,
    ToolExecutionError,
    ToolId,
)
from boba.domain.core.workspace import (
    USER_WORKSPACE_KIND,
    AllowedWorkspacesSpec,
    WorkspaceKind,
)

WORKSPACE_PARAM_NAME = "workspace"


def workspace_param_schema(spec: AllowedWorkspacesSpec) -> ParamSchema:
    """
    Собрать :class:`ParamSchema` для аргумента ``workspace``.

    Enum и default строятся из самого spec'а, хардкода имён нет. Дефолт —
    ``user`` (по значению ``USER_WORKSPACE_KIND.name``), если пользователь
    включил его в allow-list.
    """
    kinds = sorted(k.name for k in spec.kinds)
    default = (
        USER_WORKSPACE_KIND.name
        if spec.check(USER_WORKSPACE_KIND)
        else kinds[0]
    )
    return ParamSchema(
        name=WORKSPACE_PARAM_NAME,
        type=JsonType.STRING,
        description=(
            "Workspace, в котором выполняется операция. Допустимые значения: "
            + ", ".join(kinds)
            + f". По умолчанию — {default}."
        ),
        required=False,
        default=default,
        enum=tuple(kinds),
    )


def parse_workspace_arg(
    raw: Any,
    spec: AllowedWorkspacesSpec,
    tool_id: ToolId,
) -> WorkspaceKind:
    """
    Распарсить и провалидировать ``workspace`` из сырого dict-а аргументов.

    Пустое значение → дефолт (``user``, если в allow-list). Недопустимый
    kind → :class:`ToolExecutionError` с перечислением разрешённых.
    """
    if raw is None or raw == "":
        if spec.check(USER_WORKSPACE_KIND):
            return USER_WORKSPACE_KIND
        fallback = next(iter(sorted(spec.kinds, key=lambda k: k.name)), None)
        if fallback is None:
            raise ToolExecutionError(
                tool_id=tool_id,
                message="no workspaces allowed for this tool",
            )
        return fallback

    kind = WorkspaceKind(str(raw))
    if not spec.check(kind):
        allowed = sorted(k.name for k in spec.kinds)
        raise ToolExecutionError(
            tool_id=tool_id,
            message=(
                f"workspace {kind.name!r} not allowed; "
                f"allowed: {allowed}"
            ),
        )
    return kind
