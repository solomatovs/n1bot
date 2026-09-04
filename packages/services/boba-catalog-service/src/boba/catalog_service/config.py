"""Секция [catalog]: где лежат таблицы каталога и какие роли его читают и правят."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres.profile import PostgresConfig

__all__ = ["CatalogConfig"]


class CatalogConfig(BaseModel):
    """Секция [catalog]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(description="Создавать таблицы каталога при старте.")
    connection: PostgresConfig | None = Field(
        default=None,
        description='Postgres-профиль ссылкой: connection = "${postgres}".',
    )
    db_schema: str = Field(
        min_length=1,
        description="Схема postgres, в которой живут таблицы каталога.",
    )
    view_roles: tuple[str, ...] = Field(
        min_length=1,
        description="Роли, которым открыт весь каталог на чтение.",
    )
    edit_roles: tuple[str, ...] = Field(
        min_length=1,
        description="Роли, которые ведут черновики, публикуют и правят виды.",
    )

    def require_conn(self) -> PostgresConfig:
        if self.connection is None:
            msg = (
                "section [catalog]: key connection is not set, expected a postgres "
                'profile reference such as connection = "${postgres}"'
            )
            raise ValueError(msg)

        return self.connection
