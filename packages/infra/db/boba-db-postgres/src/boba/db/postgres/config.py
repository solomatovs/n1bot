"""PostgresConfig: полная libpq-модель + опции сессии + параметры пула."""

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

from boba.krb import DelegatedConfig, Kerberos, KerberosDump
from boba.toolkit.types import SecretRevealing

__all__ = ["PostgresConfig", "PostgresOptionsConfig", "PostgresPoolConfig"]


class PostgresPoolConfig(BaseModel):
    """Параметры конструктора psycopg_pool.ConnectionPool (sync/async)."""

    model_config = ConfigDict(extra="ignore")

    min_size: int = Field(default=1, description="Минимум соединений в пуле.")
    max_size: int | None = Field(
        default=None, description="Максимум соединений; None — равен min_size."
    )
    name: str | None = Field(default=None, description="Имя пула (для логов/метрик).")
    timeout: float = Field(
        default=2,
        description=(
            "Fail-fast ожидание свободного коннекта (сек); при недоступной БД "
            "getconn упадёт за это время, а не за дефолтные 30с."
        ),
    )
    max_waiting: int = Field(
        default=0, description="Предел очереди ждущих коннект; 0 — без предела."
    )
    max_lifetime: float = Field(
        default=60, description="Максимальный срок жизни соединения (сек)."
    )
    max_idle: float = Field(
        default=60,
        description="Простой соединения сверх min_size до закрытия (сек).",
    )
    reconnect_timeout: float = Field(
        default=60,
        description="Сколько пробовать восстановить коннект до отказа (сек).",
    )
    num_workers: int = Field(
        default=3, description="Число фоновых воркеров пула (reconnect/обслуживание)."
    )


class PostgresOptionsConfig(BaseModel):
    "libpq 'options': серверные GUC сессии (-c key=value); сериализуется в строку"

    model_config = ConfigDict(extra="ignore")

    statement_timeout: str | None = Field(
        default=None, description="statement_timeout, напр. '30s'."
    )
    lock_timeout: str | None = Field(
        default=None, description="lock_timeout, напр. '5s'."
    )
    idle_in_transaction_session_timeout: str | None = Field(
        default=None, description="Таймаут простоя открытой транзакции."
    )
    timezone: str | None = Field(default=None, description="TimeZone сессии.")
    default_transaction_read_only: str | None = Field(
        default=None, description="default_transaction_read_only: on|off."
    )
    search_path: str | None = Field(default=None, description="search_path сессии.")

    def to_options(self, override_options: dict[str, str] | None = None) -> str | None:
        """libpq options '-c k=v ...': GUC-поля + override; None если пусто."""
        parts = []

        for name in type(self).model_fields:
            if (value := getattr(self, name)) is not None:
                parts.append(f"-c {name}={value}")

        if override_options:
            for name, value in override_options.items():
                if value is not None:
                    parts.append(f"-c {name}={value}")

        return " ".join(parts)


class PostgresConfig(BaseModel):
    """libpq connection keywords + поведение connect() psycopg; см. PostgreSQL docs."""

    model_config = ConfigDict(extra="ignore")

    GSS_OFF: ClassVar[str] = "disable"
    """gssencmode без секции kerberos: libpq не ходит за чужим ccache."""

    GSS_REQUIRED: ClassVar[str] = "require"
    """gssencmode с секцией kerberos: сбой GSS не уходит в открытую попытку."""

    DEFAULT_KRBSRVNAME: ClassVar[str] = "postgres"
    """Имя сервиса kerberos по умолчанию у libpq."""

    # ключ контекста сериализации: пароль раскрывается только в доверенный канал
    REVEAL_SECRETS: ClassVar[str] = SecretRevealing.REVEAL_CONTEXT

    # не connect-параметры: конструктор пула, строка '-c k=v', креды kerberos
    NOT_CONNECT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"pool", "options", "kind", "kerberos"}
    )

    kind: Literal["postgres"] = Field(
        default="postgres",
        description="Дискриминатор соединения при хранении в базе.",
    )

    # libpq connection параметры
    host: str | None = Field(default=None, description="Хост(ы) или путь к сокету.")
    hostaddr: str | None = Field(default=None, description="IP хоста (без DNS).")
    port: int | None = Field(default=None, description="Порт (или сокет-суффикс).")
    dbname: str | None = Field(default=None, description="Имя БД.")
    user: str | None = Field(default=None, description="Пользователь.")
    password: SecretStr | None = Field(
        default=None,
        description="Пароль (секрет); маскируется в дампах и логах.",
    )
    passfile: str | None = Field(default=None, description="Путь к файлу паролей.")
    require_auth: str | None = Field(
        default=None, description="Требуемые методы аутентификации."
    )
    channel_binding: str | None = Field(
        default=None, description="disable|prefer|require."
    )
    connect_timeout: int | None = Field(
        default=None, description="Таймаут установки соединения (сек)."
    )
    client_encoding: str | None = Field(default=None, description="Кодировка клиента.")
    application_name: str | None = Field(
        default=None, description="application_name соединения."
    )
    fallback_application_name: str | None = Field(
        default=None, description="application_name по умолчанию."
    )
    keepalives: int | None = Field(
        default=None, description="TCP keepalive вкл/выкл (1/0)."
    )
    keepalives_idle: int | None = Field(default=None, description="TCP_KEEPIDLE (сек).")
    keepalives_interval: int | None = Field(
        default=None, description="TCP_KEEPINTVL (сек)."
    )
    keepalives_count: int | None = Field(
        default=None, description="TCP_KEEPCNT (число проб)."
    )
    tcp_user_timeout: int | None = Field(
        default=None, description="TCP_USER_TIMEOUT (мс)."
    )
    replication: str | None = Field(
        default=None, description="Режим репликации (true/database/false)."
    )
    gssencmode: str | None = Field(
        default=None, description="disable|prefer|require (GSSAPI шифрование)."
    )
    sslmode: str | None = Field(
        default=None,
        description="disable|allow|prefer|require|verify-ca|verify-full.",
    )
    sslnegotiation: str | None = Field(
        default=None, description="postgres|direct (способ начала TLS)."
    )
    sslcompression: int | None = Field(default=None, description="Сжатие TLS (1/0).")
    sslcert: str | None = Field(default=None, description="Клиентский сертификат.")
    sslkey: str | None = Field(default=None, description="Клиентский приватный ключ.")
    sslpassword: str | None = Field(
        default=None, description="Пароль приватного ключа (секрет)."
    )
    sslrootcert: str | None = Field(default=None, description="Корневой CA-сертификат.")
    sslcrl: str | None = Field(default=None, description="CRL-файл.")
    sslcrldir: str | None = Field(default=None, description="Каталог CRL.")
    sslsni: int | None = Field(default=None, description="Слать TLS SNI (1/0).")
    requirepeer: str | None = Field(
        default=None, description="Ожидаемый пользователь сервера (сокет)."
    )
    ssl_min_protocol_version: str | None = Field(
        default=None, description="Мин. версия TLS (TLSv1.2/...)."
    )
    ssl_max_protocol_version: str | None = Field(
        default=None, description="Макс. версия TLS."
    )
    krbsrvname: str | None = Field(default=None, description="Имя сервиса Kerberos.")
    gsslib: str | None = Field(default=None, description="Библиотека GSSAPI.")
    gssdelegation: int | None = Field(
        default=None, description="Делегирование GSSAPI-креденшелов (1/0)."
    )
    service: str | None = Field(
        default=None, description="Имя сервиса из pg_service.conf."
    )
    target_session_attrs: str | None = Field(
        default=None,
        description="any|read-write|read-only|primary|standby|prefer-standby.",
    )
    load_balance_hosts: str | None = Field(
        default=None, description="disable|random (балансировка по хостам)."
    )

    # поведение psycopg connect() (не libpq)
    autocommit: bool = Field(
        default=True,
        description="autocommit; для AsyncPostgresSaver.setup() обязателен.",
    )
    prepare_threshold: int | None = Field(
        default=None,
        description=(
            "Порог prepared statements; None — отключить (нужно для pgbouncer)."
        ),
    )

    # серверные опции сессии (libpq 'options'); сериализуются в строку в conn_settings
    options: Annotated[
        PostgresOptionsConfig,
        Field(
            default_factory=lambda: PostgresOptionsConfig.model_validate({}),
            description="Серверные GUC сессии (timeouts/timezone) -> libpq options.",
        ),
    ]

    # параметры пула соединений
    pool: Annotated[
        PostgresPoolConfig,
        Field(
            default_factory=lambda: PostgresPoolConfig.model_validate({}),
            description="Параметры AsyncConnectionPool.",
        ),
    ]

    # креды kerberos этого соединения; libpq keytab не принимает, его даёт libkrb5
    kerberos: Kerberos | None = Field(
        default=None,
        description=(
            "Keytab, принципал и свой ccache соединения; "
            "в конфиге подключается ссылкой ${kerberos.<name>}. "
            "В песочницу этой же секцией уезжает только готовый тикет."
        ),
    )

    def service_name(self) -> str:
        """SPN сервера в форме hostbased: <krbsrvname>@<host>; как его ищет libpq."""
        if not self.host:
            msg = "postgres connection: kerberos needs host, hostaddr is not enough"
            raise ValueError(msg)

        name = self.krbsrvname
        if not name:
            name = self.DEFAULT_KRBSRVNAME

        return f"{name}@{self.host}"

    @field_serializer("kerberos", when_used="json")
    def _dump_kerberos(
        self, value: Kerberos | None, info: SerializationInfo
    ) -> dict[str, Any] | None:
        """Дамп с раскрытыми секретами едет в песочницу: только билет вызова."""
        return KerberosDump.json(value, info.context, "postgres connection")

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

        if not context.get(PostgresConfig.REVEAL_SECRETS):
            return None

        return value.get_secret_value()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # у делегированного соединения роль — принципал сессии, он известен на вызове
        if not self.user and not isinstance(self.kerberos, DelegatedConfig):
            msg = "postgres connection: user обязателен"
            raise ValueError(msg)
        if not self.dbname:
            msg = "postgres connection: dbname обязателен"
            raise ValueError(msg)
        if not (self.host or self.hostaddr):
            msg = "postgres connection: host или hostaddr обязателен"
            raise ValueError(msg)
        if self.pool.max_size is not None and self.pool.max_size < self.pool.min_size:
            msg = (
                f"postgres connection: pool.max_size ({self.pool.max_size}) "
                f"должен быть ≥ pool.min_size ({self.pool.min_size})"
            )
            raise ValueError(msg)

        if self.kerberos is None:
            if self.gssencmode != self.GSS_OFF:
                msg = (
                    "postgres connection: without a kerberos section libpq would "
                    "use whatever ccache the process has; set "
                    f'gssencmode = "{self.GSS_OFF}" explicitly'
                )
                raise ValueError(msg)

            return self

        if self.gssencmode != self.GSS_REQUIRED:
            msg = (
                "postgres connection: a kerberos section needs "
                f'gssencmode = "{self.GSS_REQUIRED}"; with anything weaker a GSS '
                "failure silently falls back to a plain connection attempt"
            )
            raise ValueError(msg)

        if self.connect_timeout is None:
            msg = (
                "postgres connection: a kerberos section needs connect_timeout: "
                "the GSS handshake runs under a process-wide lock"
            )
            raise ValueError(msg)

        return self

    def conn_settings(
        self,
        override_options: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        "kwargs для connect(): libpq-ключи + autocommit/prepare_threshold + opts"
        conn: dict[str, Any] = {}

        for name in PostgresConfig.model_fields:
            if name in self.NOT_CONNECT_FIELDS:
                continue

            value = getattr(self, name)
            if value is None:
                continue

            if isinstance(value, SecretStr):
                conn[name] = value.get_secret_value()
                continue

            conn[name] = value

        if opts := self.options.to_options(override_options):
            conn["options"] = opts

        return conn

    def with_schema(self, schema: str) -> PostgresConfig:
        """Копия профиля с search_path сервиса."""
        options = self.options.model_copy(update={"search_path": schema})
        return self.model_copy(update={"options": options})

    def pool_settings(self) -> dict[str, Any]:
        """kwargs конструктора ConnectionPool (без None)."""
        res: dict[str, Any] = {}
        for name in type(self.pool).model_fields:
            if (value := getattr(self.pool, name)) is not None:
                res[name] = value
        return res
