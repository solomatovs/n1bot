"""Поведение HTTP-транспорта LLM-провайдеров: таймауты, ретраи, пул, дампы.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["HttpConfig", "HttpDumpConfig"]


class HttpDumpConfig(BaseModel):
    """Дамп HTTP-обмена с провайдером: флаг и каталог файлов."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Писать HTTP-обмен с провайдером в path.",
    )

    path: str = Field(
        default="",
        description="Каталог дампов; обязателен при enable = true.",
    )

    @model_validator(mode="after")
    def _validate_path(self) -> Self:
        if not self.enable:
            return self

        if not self.path:
            msg = (
                "section [http.dump]: enable = true requires a dump directory "
                f"in path, got path={self.path!r}"
            )
            raise ValueError(msg)

        return self


class HttpConfig(BaseModel):
    """Поведение httpx-транспорта; адрес endpoint'а живёт в конфиге провайдера."""

    model_config = ConfigDict(extra="ignore")

    ssl_verify: bool = Field(
        default=True,
        description="Проверять TLS-сертификат сервера.",
    )

    dump: HttpDumpConfig = Field(
        default_factory=HttpDumpConfig,
        description="Дамп HTTP-обмена с провайдером.",
    )

    trust_env: bool = Field(
        default=True,
        description=(
            "Брать прокси и CA из окружения (HTTPS_PROXY, SSL_CERT_FILE); "
            "false — окружение игнорируется целиком."
        ),
    )

    proxy: str = Field(
        default="",
        description="Прокси для запросов к провайдеру; пусто — напрямую.",
    )

    http2: bool = Field(
        default=False,
        description="Разрешить HTTP/2 к провайдеру.",
    )

    stream_chunk_timeout: float = Field(
        default=600,
        ge=0,
        description=(
            "Пауза между чанками контента в стриме, секунды: считает разрывы "
            "между разобранными чанками, keepalive её не сбрасывает. "
            "0 — без потолка."
        ),
    )

    max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Повторы запроса при 429/5xx и таймауте; "
            "каждый повтор — новая полная попытка."
        ),
    )

    connect_timeout: float = Field(
        default=5,
        description="установка TCP-соединения с хостом (включая TLS handshake)",
    )

    read_timeout: float = Field(
        default=600,
        description=(
            "ожидание очередных байт от сервера; при stream=True — пауза "
            "между байтами, а не между чанками контента"
        ),
    )

    write_timeout: float = Field(
        default=100,
        description="отправка тела запроса на сервер",
    )

    pool_timeout: float = Field(
        default=5,
        description=("ожидание свободного соединения из пула httpx (когда все заняты)"),
    )

    max_connections: int = Field(
        default=50,
        description="",
    )

    max_keepalive_connections: int = Field(
        default=10,
        description="",
    )

    keepalive_expiry: float = Field(
        default=5,
        description="",
    )

    retries: int = Field(
        default=3,
        description="Число повторов установления соединения в httpx-транспорте.",
    )

    tcp_keepalive: bool = Field(
        default=True,
        description=(
            "TCP keepalive (SO_KEEPALIVE): защита от молчаливого разрыва "
            "простаивающего соединения файрволом (обычно режут после 5-10 минут "
            "тишины)."
        ),
    )

    tcp_keepidle: int = Field(
        default=60,
        description=(
            "TCP_KEEPIDLE: секунд простоя, после которых ядро шлёт "
            "keepalive-пробу. Без явного значения берётся sysctl "
            "tcp_keepalive_time — обычно 7200 (2 часа простоя!)."
        ),
    )

    tcp_keepintvl: int = Field(
        default=10,
        description="TCP_KEEPINTVL: интервал между повторными пробами (сек).",
    )

    tcp_keepcnt: int = Field(
        default=10,
        description=(
            "TCP_KEEPCNT: число безответных проб, после которых соединение "
            "считается мёртвым; следующая работа с сокетом даст "
            "ECONNABORTED/ETIMEDOUT."
        ),
    )

    tcp_user_timeout: int = Field(
        default=0,
        ge=0,
        description=(
            "TCP_USER_TIMEOUT: сколько миллисекунд ядро ждёт подтверждения "
            "уже отправленных данных, прежде чем оборвать соединение; в "
            "отличие от keepalive работает и на активном обмене. 0 — не задавать."
        ),
    )
