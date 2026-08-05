"""Контракт clickhouse-payload'а: SQL в песочнице, секреты едут через stdin."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.db.clickhouse import ClickHouseConfig

__all__ = [
    "ChQueryRequest",
    "ChQueryTrailer",
]


class ChCall(BaseModel):
    """Общая часть: к чему подключаться и что выполнять."""

    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    connection: ClickHouseConfig = Field(
        description=(
            "Профиль подключения целиком: адрес, TLS, настройки сессии и "
            "креды kerberos, по которым payload сам получает TGT."
        ),
    )
    sql: str = Field(min_length=1)

    @field_serializer("connection", when_used="json")
    def _dump_connection(self, value: ClickHouseConfig) -> dict[str, Any]:
        """stdin песочницы — доверенный канал: только здесь пароль едет раскрытым."""
        return value.model_dump(
            mode="json",
            context={ClickHouseConfig.REVEAL_SECRETS: True},
        )


class ChQueryRequest(ChCall):
    """Запрос строк с лимитом."""

    OP: ClassVar[str] = "ch_query"

    params: dict[str, Any] = Field(
        description=(
            "Именованные параметры запроса под подстановку {name:Type}; "
            "пустой словарь — без них."
        ),
    )
    row_limit: int = Field(ge=1)


class ChQueryTrailer(BaseModel):
    """Итог запроса: строки ушли кадрами, здесь признак превышения лимита."""

    model_config = ConfigDict(extra="forbid")

    truncated: bool
