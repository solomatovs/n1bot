"""Политика сроков и поколения сессий приложения."""

from __future__ import annotations

import secrets
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.lifetime import SessionLifetime


class SessionPolicy(BaseModel):
    """Сроки и поколение сессий; секреты подписи задаёт транспортный адаптер."""

    model_config = ConfigDict(extra="forbid")

    session_ttl_sec: int = Field(gt=0, description="Срок токена сессии.")
    session_max_sec: int = Field(
        gt=0,
        description=(
            "Потолок сессии от первого входа: дольше без нового входа не продлить."
        ),
    )
    generation: str = Field(
        default="",
        description=(
            "Поколение сессий: токены другого поколения отвергаются. Пусто — "
            "случайное на каждый старт процесса, и рестарт разлогинивает всех; "
            "задано — общее для приложений, которые должны принимать токены "
            "друг друга."
        ),
    )

    @model_validator(mode="after")
    def _max_covers_ttl(self) -> Self:
        if self.session_max_sec < self.session_ttl_sec:
            msg = (
                "[session]: session_max_sec must not be shorter than "
                f"session_ttl_sec, got session_max_sec = {self.session_max_sec} "
                f"and session_ttl_sec = {self.session_ttl_sec}"
            )
            raise ValueError(msg)

        return self

    _process_generation: ClassVar[str] = ""
    """Случайное поколение процесса: одно на все читатели токенов в нём."""

    def session_generation(self) -> str:
        """Поколение из конфига либо случайное, общее для процесса."""
        if self.generation:
            return self.generation

        if not SessionPolicy._process_generation:
            SessionPolicy._process_generation = secrets.token_hex(16)

        return SessionPolicy._process_generation

    def lifetime(self) -> SessionLifetime:
        return SessionLifetime(
            ttl_sec=self.session_ttl_sec, max_sec=self.session_max_sec
        )
