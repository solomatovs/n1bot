"""Сроки подключения и вызова новых пользовательских MCP-серверов."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.crypto.config import ValueCipherConfig


class McpDefaultsConfig(BaseModel):
    """Сроки новых MCP-серверов; форма копирует их в запись сервера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_timeout_sec: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    call_timeout_sec: float = Field(default=60.0, gt=0, allow_inf_nan=False)


class McpServersConfig(McpDefaultsConfig, ValueCipherConfig):
    """Шифрование каталога MCP-серверов и сроки новых подключений."""

    SECTION: ClassVar[str] = "mcp_servers"
