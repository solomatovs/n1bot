"""HttpConnection: переиспользуемый транспортный профиль (всё кроме URL).

Содержит параметры открытия HTTP-соединения и выполнения запроса —
timeout/ssl/retry + auth — но НЕ url (его даёт consumer: web-tool из
аргумента LLM, confluence из своего `base_url`). Один и тот же профиль
переиспользуется и web-tool'ом (dict по hostname), и confluence
(один профиль + base_url) — это «web-профиль».
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.transport.http.auth import NoneAuth, WebAuth

__all__ = ["HttpConnection"]


class HttpConnection(BaseModel):
    """Транспортный профиль: timeout/ssl/retry + auth. Без url."""

    model_config = ConfigDict(extra="ignore")

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
    auth: WebAuth = Field(
        default=NoneAuth(method="none"),
        description=(
            "Auth-метод inline: `{ method = 'none'|'basic'|'bearer'|'digest', "
            "... }`. По умолчанию anonymous (`method='none'`)."
        ),
    )

    def make_client(
        self, *, base_url: str = "", headers: Mapping[str, str] | None = None
    ) -> httpx.Client:
        """`httpx.Client` из профиля: timeout/ssl/auth — единое место сборки.

        Используется обоими путями: `HttpTransport` (pipeline-streaming, передаёт
        per-request `headers`) и прямыми потребителями вроде курсорной REST-
        пагинации (`base_url` — опора для относительных path'ов). Retry — на
        стороне потребителя, по `retry_attempts`/`retry_backoff_sec` профиля.
        """
        return httpx.Client(
            base_url=base_url,
            timeout=self.timeout_sec,
            verify=self.ssl_verify,
            auth=self.auth.httpx_auth(),
            headers=headers,
        )
