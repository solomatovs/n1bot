"""Настройки узла, сроков владения и выбранного транспорта сообщений.

Ошибки:
ValueError — имя экземпляра или соотношение сроков недопустимо."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


@dataclass(frozen=True)
class AppName:
    """Имя, которое приложение передаёт для регистрации своего инстанса."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("application name must not be empty")

        for char in self.value:
            if char.isascii() and (char.isalnum() or char in "_.-"):
                continue

            raise ValueError(
                f"application name {self.value!r}: expected ASCII letters, "
                "digits, underscores, dots or hyphens"
            )


class ClusterConfig(BaseModel):
    """Секция [cluster]: имя узла, из которого с именем приложения складывается имя
    инстанса, и сроки жизни блокировок, слушателя шины и хранимых событий.
    """

    model_config = ConfigDict(extra="forbid")

    SEPARATOR: ClassVar[str] = "-"

    node_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    host: str = Field(min_length=1, description="Узел, где лежат журналы инструментов.")
    lock_ttl_sec: int = Field(
        default=20, gt=0, description="Срок блокировки без подтверждения жизни."
    )
    heartbeat_sec: int = Field(
        default=6, gt=0, description="Период подтверждения жизни держателем."
    )
    reaper_period_sec: int = Field(
        default=10, gt=0, description="Период сторожа протухших блокировок."
    )
    queue_usage_limit: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Доля очереди NOTIFY, при которой слушатель переподключается.",
    )
    retention_sec: int = Field(
        default=3600,
        gt=0,
        description="Сколько хранить события и тела областей, в которых тихо.",
    )

    @model_validator(mode="after")
    def _heartbeat_fits_ttl(self) -> Self:
        if self.heartbeat_sec * 2 > self.lock_ttl_sec:
            msg = (
                "[cluster]: heartbeat_sec must be at most half of lock_ttl_sec, "
                f"got heartbeat_sec = {self.heartbeat_sec} and "
                f"lock_ttl_sec = {self.lock_ttl_sec}"
            )
            raise ValueError(msg)

        return self

    def instance_of(self, app: AppName) -> str:
        return f"{self.node_id}{self.SEPARATOR}{app.value}"


class MessagingConfig(BaseModel):
    """Общие поля выбранного транспорта; конфиг реализации разбирает её фабрика."""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)
