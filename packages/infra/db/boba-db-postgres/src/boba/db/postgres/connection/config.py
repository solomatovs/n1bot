"""PostgresConfig: полная libpq-модель + опции сессии + параметры пула."""

from __future__ import annotations

from enum import StrEnum
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

from boba.connections.base import ClientIdentity, ConnectionBase
from boba.db.postgres.connection.auth import (
    PostgresAuth,
    PostgresAuthSession,
    PostgresKerberos,
    PostgresLibpq,
)
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


class CopyText(StrEnum):
    """Настройки сессии, от которых зависит текст COPY: с ними один и тот же
    запрос печатает одно и то же на любом сервере и в любой базе. DateStyle —
    ISO-даты и порядок год-месяц-день на вводе; IntervalStyle — интервалы в
    записи postgres; TimeZone UTC — смещение +00 у timestamptz; bytea hex;
    деньги без локали; float кратчайшим точным текстом; UTF-8; bytea внутри
    xml base64; строковые литералы запроса по стандарту."""

    DATESTYLE = "ISO,YMD"
    INTERVALSTYLE = "postgres"
    TIMEZONE = "UTC"
    BYTEA_OUTPUT = "hex"
    LC_MONETARY = "C"
    EXTRA_FLOAT_DIGITS = "3"
    CLIENT_ENCODING = "UTF8"
    XMLBINARY = "base64"
    STANDARD_CONFORMING_STRINGS = "on"


class CopySession(BaseModel):
    """Настройки сессии COPY, которые задаёт вызов насоса: по умолчанию —
    зафиксированный текст CopyText, вызывающий (LLM) меняет их, когда поток
    нужен в другой кодировке, локали денег, точности float или записи дат.
    Значения уходят в libpq options и перекрывают профиль соединения."""

    model_config = ConfigDict(frozen=True)

    datestyle: str = Field(
        default=CopyText.DATESTYLE.value,
        min_length=1,
        description="DateStyle: ISO,YMD — ISO-даты, год-месяц-день на вводе.",
    )
    intervalstyle: str = Field(
        default=CopyText.INTERVALSTYLE.value,
        min_length=1,
        description="IntervalStyle: postgres, postgres_verbose, sql_standard, iso_8601",
    )
    timezone: str = Field(
        default=CopyText.TIMEZONE.value,
        min_length=1,
        description="TimeZone сессии: смещение timestamptz в тексте, UTC даёт +00.",
    )
    bytea_output: str = Field(
        default=CopyText.BYTEA_OUTPUT.value,
        min_length=1,
        description="bytea_output: hex | escape.",
    )
    lc_monetary: str = Field(
        default=CopyText.LC_MONETARY.value,
        min_length=1,
        description="Локаль money: C — без символа валюты и разделителей.",
    )
    extra_float_digits: int = Field(
        default=int(CopyText.EXTRA_FLOAT_DIGITS.value),
        ge=-15,
        le=3,
        description=(
            "extra_float_digits: 3 — float печатается кратчайшим точным текстом "
            "на любой версии; меньше — короче и с потерей."
        ),
    )
    client_encoding: str = Field(
        default=CopyText.CLIENT_ENCODING.value,
        min_length=1,
        description="Кодировка текста потока: UTF8, WIN1251, LATIN1.",
    )
    xmlbinary: str = Field(
        default=CopyText.XMLBINARY.value,
        min_length=1,
        description="xmlbinary: base64 | hex — bytea внутри xml.",
    )
    standard_conforming_strings: str = Field(
        default=CopyText.STANDARD_CONFORMING_STRINGS.value,
        min_length=1,
        description="standard_conforming_strings: on | off.",
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
    datestyle: str | None = Field(default=None, description="DateStyle сессии.")
    intervalstyle: str | None = Field(default=None, description="IntervalStyle.")
    bytea_output: str | None = Field(
        default=None, description="bytea_output: hex|escape."
    )
    lc_monetary: str | None = Field(default=None, description="Локаль money.")
    extra_float_digits: str | None = Field(
        default=None,
        description="extra_float_digits: 3 — float печатается точно и до 12-й версии.",
    )
    client_encoding: str | None = Field(default=None, description="Кодировка сессии.")
    xmlbinary: str | None = Field(default=None, description="xmlbinary: base64|hex.")
    standard_conforming_strings: str | None = Field(
        default=None, description="standard_conforming_strings: on|off."
    )

    def copy_text(self) -> PostgresOptionsConfig:
        """Те же опции с зафиксированным текстом COPY (CopyText): для дампов и
        загрузок, у которых текст потока не настраивается."""
        return self.copy_session(CopySession())

    def copy_session(self, session: CopySession) -> PostgresOptionsConfig:
        """Те же опции с настройками сессии COPY из вызова насоса."""
        return self.model_copy(
            update={
                "datestyle": session.datestyle,
                "intervalstyle": session.intervalstyle,
                "timezone": session.timezone,
                "bytea_output": session.bytea_output,
                "lc_monetary": session.lc_monetary,
                "extra_float_digits": str(session.extra_float_digits),
                "client_encoding": session.client_encoding,
                "xmlbinary": session.xmlbinary,
                "standard_conforming_strings": session.standard_conforming_strings,
            }
        )

    def to_options(self) -> str | None:
        """libpq options '-c k=v ...' по заполненным GUC-полям; None если пусто.
        Пробел и обратный слэш в значении экранируются, как требует libpq."""
        parts = []

        for name in type(self).model_fields:
            if (value := getattr(self, name)) is not None:
                escaped = str(value).replace("\\", "\\\\").replace(" ", "\\ ")
                parts.append(f"-c {name}={escaped}")

        return " ".join(parts)


class ApplicationName:
    """Подпись сессии для postgres: application_name режется до 63 байт.

    Длиннее сервер обрезает сам, и в журнале остаётся кусок без имени
    инструмента, поэтому режем осознанно — по границе байтов utf-8.
    """

    MAX_BYTES: ClassVar[int] = 63
    SEPARATOR: ClassVar[str] = ":"

    @classmethod
    def of(cls, client: ClientIdentity) -> str:
        joined = cls.SEPARATOR.join((client.application, client.login, client.tool))
        raw = joined.encode("utf-8")
        if len(raw) <= cls.MAX_BYTES:
            return joined

        return raw[: cls.MAX_BYTES].decode("utf-8", errors="ignore")


class PostgresConfig(ConnectionBase):
    """libpq connection keywords + поведение connect() psycopg; см. PostgreSQL docs."""

    model_config = ConfigDict(extra="ignore")

    # не connect-параметры: конструктор пула, строка '-c k=v', способ авторизации
    NOT_CONNECT_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"pool", "options", "auth"}
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
            msg = (
                f"postgres connection to {self.hostaddr!r}: kerberos SPN needs host, "
                "hostaddr alone is not enough"
            )
            raise ValueError(msg)

        if not isinstance(self.auth, KerberosAuthBase):
            msg = (
                f"postgres connection to {self.host}: auth {self.auth.method} "
                "has no kerberos service name, expected a kerberos_* auth"
            )
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

    def auth_session(self) -> PostgresAuthSession:
        """Окружение авторизации этого профиля на время connect."""
        return PostgresAuthSession(self.auth, self.where())

    def where(self) -> str:
        """host:port/dbname соединения для текста ошибок и журнала."""
        host = self.host
        if not host:
            host = self.hostaddr

        return f"{host}:{self.port}/{self.dbname}"

    def labeled(self, client: ClientIdentity) -> PostgresConfig:
        """Подпись сессии в application_name: его же показывает pg_stat_activity."""
        return self.model_copy(update={"application_name": ApplicationName.of(client)})

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
            msg = (
                "postgres connection: dbname must be a non-empty database name, "
                f"got {self.dbname!r}"
            )
            raise ValueError(msg)

        if not (self.host or self.hostaddr):
            msg = (
                "postgres connection: host or hostaddr must be set, "
                f"got host={self.host!r} hostaddr={self.hostaddr!r}"
            )
            raise ValueError(msg)

        if self.pool.max_size is not None and self.pool.max_size < self.pool.min_size:
            msg = (
                f"postgres connection to {self.host}: pool.max_size "
                f"({self.pool.max_size}) must be >= pool.min_size "
                f"({self.pool.min_size})"
            )
            raise ValueError(msg)

        if not isinstance(self.auth, KerberosAuthBase):
            return self

        if self.connect_timeout is None:
            msg = (
                f"postgres connection to {self.host}: auth {self.auth.method} "
                "requires connect_timeout (the GSS handshake runs under a "
                "process-wide lock), got none"
            )
            raise ValueError(msg)

        return self

    def copy_text(self) -> PostgresConfig:
        """Тот же профиль с зафиксированным текстом COPY (CopyText): для сессий,
        которые отдают или принимают вывод сервера как есть — дампов скраперов
        и загрузки в ix."""
        return self.copy_session(CopySession())

    def copy_session(self, session: CopySession) -> PostgresConfig:
        """Тот же профиль с настройками сессии COPY из вызова насоса."""
        return self.model_copy(update={"options": self.options.copy_session(session)})

    def conn_settings(self) -> dict[str, Any]:
        "kwargs для connect(): libpq-ключи + autocommit/prepare_threshold + opts"
        conn: dict[str, Any] = {}

        for name in PostgresConfig.model_fields:
            if name in self.NOT_CONNECT_FIELDS:
                continue

            if name in self.common_fields():
                continue

            value = getattr(self, name)
            if value is None:
                continue

            if isinstance(value, SecretStr):
                conn[name] = value.get_secret_value()
                continue

            conn[name] = value

        conn.update(PostgresLibpq.of(self.auth))

        if opts := self.options.to_options():
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
