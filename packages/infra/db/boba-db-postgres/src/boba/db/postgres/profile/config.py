"""PostgresConfig: полная libpq-модель + опции сессии + параметры пула."""

from __future__ import annotations

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

from boba.connections.base import ConnectionProfileBase
from boba.db.postgres.profile.auth import PostgresAuth, PostgresKerberos, PostgresLibpq
from boba.kerberos import KerberosAuthBase, KerberosDump, TicketAuth

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


class PostgresConfig(ConnectionProfileBase):
    """libpq connection keywords + поведение connect() psycopg; см. PostgreSQL docs."""

    model_config = ConfigDict(extra="ignore")

    # не connect-параметры: конструктор пула, строка '-c k=v', способ авторизации
    NOT_CONNECT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"pool", "options", "kind", "auth"}
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
    sslmode: str | None = Field(
        default=None,
        description="disable|allow|prefer|require|verify-ca|verify-full.",
    )
    sslnegotiation: str | None = Field(
        default=None, description="postgres|direct (способ начала TLS)."
    )
    sslcompression: int | None = Field(default=None, description="Сжатие TLS (1/0).")
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

    # способ аутентификации: одно поле, из него выводятся ключи libpq
    auth: PostgresAuth = Field(
        description=(
            "Как аутентифицируемся: trust | password | certificate | "
            "kerberos_keytab | kerberos_password | kerberos_delegated. "
            "Поля задаёт сам вариант; gssencmode, require_auth, krbsrvname и "
            "роль kerberos-варианта выводятся из него."
        ),
    )

    def service_name(self) -> str:
        """SPN сервера в форме hostbased: <service>@<host>; как его ищет libpq."""
        if not self.host:
            msg = "postgres connection: kerberos needs host, hostaddr is not enough"
            raise ValueError(msg)

        if not isinstance(self.auth, KerberosAuthBase):
            msg = f"postgres connection: {self.auth.method} has no kerberos service"
            raise ValueError(msg)

        return f"{PostgresKerberos.service_of(self.auth)}@{self.host}"

    def kerberos_section(self) -> KerberosAuthBase | None:
        if isinstance(self.auth, KerberosAuthBase):
            return self.auth

        return None

    def with_call_ticket(self, ticket: TicketAuth) -> PostgresConfig:
        return self.model_copy(update={"auth": ticket})

    def trace(self) -> str:
        return self.auth.trace()

    def labeled(self, label: str) -> PostgresConfig:
        return self.model_copy(update={"application_name": label})


    @field_serializer("auth", when_used="json")
    def _dump_auth(
        self, value: PostgresAuth, info: SerializationInfo
    ) -> dict[str, Any] | None:
        """Дамп с раскрытыми секретами едет в песочницу: kerberos — только билетом."""
        if isinstance(value, KerberosAuthBase):
            return KerberosDump.json(value, info.context, "postgres connection")

        return value.model_dump(mode="json", context=info.context)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        # у делегированного соединения роль — принципал сессии, он известен на вызове
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

        if not isinstance(self.auth, KerberosAuthBase):
            return self

        if self.connect_timeout is None:
            msg = (
                f"postgres connection: {self.auth.method} needs connect_timeout: "
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

        conn.update(PostgresLibpq.of(self.auth))

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
