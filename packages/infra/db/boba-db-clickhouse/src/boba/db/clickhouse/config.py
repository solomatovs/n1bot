"""ClickHouseConfig: параметры HTTP-клиента clickhouse-connect + настройки сессии."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    field_serializer,
    model_validator,
)

from boba.krb import KeytabConfig
from boba.toolkit.types import SecretRevealing

__all__ = ["ClickHouseConfig", "ClickHouseSettingsConfig"]


class ClickHouseSettingsConfig(BaseModel):
    """Серверные настройки сессии ClickHouse; едут параметрами каждого запроса."""

    model_config = ConfigDict(extra="ignore")

    readonly: int | None = Field(
        default=None,
        description=(
            "0 — без ограничений, 1 — только чтение без права менять настройки, "
            "2 — только чтение с правом менять настройки сессии."
        ),
    )
    max_execution_time: int | None = Field(
        default=None, description="Потолок времени запроса (сек)."
    )
    max_result_rows: int | None = Field(
        default=None, description="Серверный потолок строк результата."
    )
    result_overflow_mode: str | None = Field(
        default=None, description="throw|break — что делать при max_result_rows."
    )
    max_threads: int | None = Field(
        default=None, description="Потолок потоков на запрос."
    )
    max_memory_usage: int | None = Field(
        default=None, description="Потолок памяти на запрос (байт)."
    )
    max_block_size: int | None = Field(
        default=None,
        description="Размер блока в строках; им же режется потоковая выдача.",
    )
    session_timezone: str | None = Field(
        default=None, description="Таймзона сессии, напр. 'UTC'."
    )

    def to_settings(self) -> dict[str, Any]:
        """Настройки без None; пустой dict — сессия остаётся серверной по умолчанию."""
        settings: dict[str, Any] = {}
        for name in type(self).model_fields:
            if (value := getattr(self, name)) is not None:
                settings[name] = value
        return settings


class ClickHouseConfig(BaseModel):
    """Параметры clickhouse_connect.AsyncClient (HTTP-интерфейс) + настройки сессии."""

    model_config = ConfigDict(extra="ignore")

    # ключ контекста сериализации: пароль раскрывается только в доверенный канал
    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    # не аргументы конструктора клиента: настройки сессии и креды kerberos
    NOT_CLIENT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"kind", "settings", "kerberos", "krbsrvname"}
    )

    kind: Literal["clickhouse"] = Field(
        default="clickhouse",
        description="Дискриминатор соединения при хранении в базе.",
    )

    # адрес и аутентификация
    host: str | None = Field(default=None, description="Хост или IP сервера.")
    port: int | None = Field(default=None, description="Порт HTTP-интерфейса.")
    interface: Literal["http", "https"] | None = Field(
        default=None, description="http или https; TLS включается именно здесь."
    )
    database: str | None = Field(default=None, description="База по умолчанию.")
    username: str | None = Field(
        default=None,
        description="Пользователь ClickHouse; при kerberos не задаётся.",
    )
    password: SecretStr | None = Field(
        default=None,
        description="Пароль (секрет); маскируется в дампах и логах.",
    )

    # поведение HTTP-клиента
    connect_timeout: int | None = Field(
        default=None, description="Таймаут установки соединения (сек)."
    )
    send_receive_timeout: int | None = Field(
        default=None, description="Таймаут чтения ответа (сек)."
    )
    query_limit: int | None = Field(
        default=None, description="LIMIT клиента на строки; 0 — без лимита."
    )
    query_retries: int | None = Field(
        default=None, description="Повторы запроса при сетевом сбое."
    )
    compress: bool | str | None = Field(
        default=None, description="True или lz4|zstd|br|gzip — сжатие ответа."
    )
    client_name: str | None = Field(
        default=None, description="Префикс User-Agent; виден в system.query_log."
    )
    session_id: str | None = Field(default=None, description="Идентификатор сессии.")
    connector_limit: int | None = Field(
        default=None, description="Потолок соединений клиента."
    )
    connector_limit_per_host: int | None = Field(
        default=None, description="Потолок соединений на хост."
    )
    keepalive_timeout: float | None = Field(
        default=None, description="Простой keepalive-соединения до закрытия (сек)."
    )

    # TLS
    verify: bool | None = Field(
        default=None, description="Проверять сертификат сервера в https."
    )
    ca_cert: str | None = Field(default=None, description="Корневой CA-сертификат.")
    client_cert: str | None = Field(
        default=None, description="Клиентский сертификат (.pem)."
    )
    client_cert_key: str | None = Field(
        default=None, description="Приватный ключ клиентского сертификата."
    )
    server_host_name: str | None = Field(
        default=None,
        description=(
            "Имя сервера для TLS-проверки и заголовка Host; оно же берётся "
            "в SPN, когда host задан адресом."
        ),
    )
    http_proxy: str | None = Field(default=None, description="Прокси для http.")
    https_proxy: str | None = Field(default=None, description="Прокси для https.")

    # серверные настройки сессии
    settings: Annotated[
        ClickHouseSettingsConfig,
        Field(
            default_factory=lambda: ClickHouseSettingsConfig.model_validate({}),
            description="Настройки сессии (readonly/таймауты/лимиты).",
        ),
    ]

    # креды kerberos этого соединения; SPNEGO-токен строится по ним на каждый запрос
    kerberos: KeytabConfig | None = Field(
        default=None,
        description=(
            "Keytab, принципал и свой ccache соединения; "
            "в конфиге подключается ссылкой ${kerberos.<name>}."
        ),
    )
    krbsrvname: str | None = Field(
        default=None,
        description=(
            "Имя сервиса Kerberos, для HTTP-интерфейса ClickHouse — HTTP. "
            "SPN собирается как <krbsrvname>@<server_host_name или host>."
        ),
    )

    @field_serializer("password", when_used="json")
    def _dump_password(
        self, value: SecretStr | None, info: SerializationInfo
    ) -> str | None:
        """Пароль уходит в дамп только с REVEAL_SECRETS в контексте, иначе его нет."""
        if value is None:
            return None

        context = info.context
        if not isinstance(context, Mapping):
            return None

        if not context.get(ClickHouseConfig.REVEAL_SECRETS):
            return None

        return value.get_secret_value()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.host:
            msg = "clickhouse connection: host обязателен"
            raise ValueError(msg)
        if not self.port:
            msg = "clickhouse connection: port обязателен"
            raise ValueError(msg)
        if not self.interface:
            msg = "clickhouse connection: interface обязателен (http или https)"
            raise ValueError(msg)

        if self.kerberos is None:
            if not self.username:
                msg = (
                    "clickhouse connection: username обязателен, когда секции "
                    "kerberos нет"
                )
                raise ValueError(msg)
            return self

        if not self.krbsrvname:
            msg = (
                "clickhouse connection: секция kerberos требует krbsrvname "
                '(для HTTP-интерфейса ClickHouse — krbsrvname = "HTTP")'
            )
            raise ValueError(msg)
        if self.password is not None:
            msg = (
                "clickhouse connection: password и kerberos взаимоисключающи — "
                "заголовок Negotiate замещает basic-аутентификацию"
            )
            raise ValueError(msg)

        return self

    def service_name(self) -> str:
        """SPN сервиса в форме hostbased: krbsrvname@<имя сервера>."""
        if not self.krbsrvname:
            msg = "clickhouse connection: krbsrvname не задан"
            raise ValueError(msg)
        return f"{self.krbsrvname}@{self.server_host_name or self.host}"

    def client_settings(self) -> dict[str, Any]:
        """kwargs конструктора AsyncClient: адрес, TLS, таймауты и settings сессии."""
        client: dict[str, Any] = {}

        for name in ClickHouseConfig.model_fields:
            if name in self.NOT_CLIENT_FIELDS:
                continue

            value = getattr(self, name)
            if value is None:
                continue

            if isinstance(value, SecretStr):
                client[name] = value.get_secret_value()
                continue

            client[name] = value

        client["settings"] = self.settings.to_settings()
        return client

    def with_database(self, database: str) -> ClickHouseConfig:
        """Копия профиля с другой базой по умолчанию."""
        return self.model_copy(update={"database": database})
