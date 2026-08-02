"""HttpConnection: переиспользуемый транспортный профиль (всё кроме URL).

Содержит параметры открытия HTTP-соединения и выполнения запроса —
timeout/ssl/retry + auth — но НЕ url (его даёт consumer: web-tool из
аргумента LLM, confluence из своего base_url). Один и тот же профиль
переиспользуется и web-tool'ом (dict по hostname), и confluence
(один профиль + base_url) — это «web-профиль».
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from boba.transport.http.auth import NoneAuth, WebAuth

__all__ = ["HttpProfile"]


class HttpProfile(BaseModel):
    """Транспортный профиль: timeout/ssl/retry + auth. Без url."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["web"] = Field(
        default="web",
        description="Дискриминатор соединения при хранении в базе.",
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Базовый URL для всех запросов с этим профилем (например, `https://api.example.com/v1/`)"
        ),
    )
    auth: WebAuth = Field(
        default=NoneAuth(method="none"),
        description=(
            "Auth-метод inline: `{ method = 'none'|'basic'|'bearer'|'digest', "
            "... }`. По умолчанию anonymous (`method='none'`)."
        ),
    )

    timeout_sec: float = Field(
        default=30.0,
        gt=0,
        description="HTTP-таймаут запроса (сек).",
    )
    ssl_verify: bool = Field(
        default=True,
        description="Проверять ли TLS-сертификат (false — для self-signed).",
    )
    retry_attempts: int = Field(
        default=1,
        ge=1,
        description=(
            "Сколько раз пытаться выполнить запрос. Ретраятся 5xx и "
            "transport-ошибки (timeout/connect); 4xx — нет. 1 — без retry."
        ),
    )
    retry_backoff_sec: float = Field(
        default=1.0,
        ge=0,
        description="Базовый линейный backoff между попытками (сек) × номер попытки.",
    )
