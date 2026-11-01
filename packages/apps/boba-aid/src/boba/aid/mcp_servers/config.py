"""Сроки подключения и вызова новых пользовательских MCP-серверов."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class McpUserConfig(BaseModel):
    """Секция mcp_user; McpForms копирует сроки в новую запись сервера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "mcp_user"

    connect_timeout_sec: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    call_timeout_sec: float = Field(default=60.0, gt=0, allow_inf_nan=False)
