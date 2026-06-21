from typing import Annotated, Any, Literal

from chainlit.config import ChainlitConfig
from pydantic import BaseModel, ConfigDict, Field

LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": True,
        },
        "uvcorn": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": True,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "uvcorn": {
            "formatter": "uvcorn",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {"handlers": ["default"], "level": "INFO"},
    "loggers": {
        "uvicorn": {"handlers": ["uvcorn"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


class OpenAiConfig(BaseModel):
    """Транспорт openai-совместимого провайдера: endpoint + httpx-тюнинг."""

    model_config = ConfigDict(extra="ignore")

    base_url: Annotated[
        str,
        Field(description="Endpoint openai-совместимого API."),
    ]

    api_key: Annotated[
        str,
        Field(description="Ключ API провайдера."),
    ]

    ssl_verify: bool = Field(
        default=True,
        description="Проверять TLS-сертификат сервера.",
    )

    connect_timeout: float = Field(
        default=5,
        description="установка TCP-соединения с хостом (включая TLS handshake)",
    )

    read_timeout: float = Field(
        default=100,
        description=(
            "ожидание данных от сервера; при stream=True — пауза между чанками"
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


class AgentProfile(BaseModel):
    """Профиль агента (копия agent.* из конфига): LLM-провайдер + модель + workspace."""

    model_config = ConfigDict(extra="ignore")

    openai: Annotated[
        OpenAiConfig,
        Field(
            description=(
                "Транспорт openai-провайдера; в конфиге подключается ссылкой "
                "${openai.<name>}."
            ),
        ),
    ]

    model: Annotated[
        str,
        Field(description="Имя LLM-модели у выбранного провайдера."),
    ]

    default_system_prompt: str = Field(
        default="",
        description="Системный промпт по умолчанию",
    )

    temperature: float = Field(
        default=0,
        description="",
    )

    max_tokens: int = Field(
        default=2500,
        description="",
    )

    top_p: float = Field(
        default=1,
        description="",
    )

    frequency_penalty: float = Field(
        default=0,
        description="",
    )

    presence_penalty: float = Field(
        default=0,
        description="",
    )

    stop: Annotated[
        list[str],
        Field(default_factory=lambda: ["```"], description=""),
    ]


class ChainlitExtendConfig(ChainlitConfig):
    ping_interval: int = Field(
        default=300,
        description=(
            "даём клиенту пережить долгие паузы(engine.io heartbeat)"
            "удобно для debug при длительных breakpoint'ах"
        ),
    )

    ping_timeout: int = Field(
        default=300,
        description="",
    )

    max_decode_packets: int = Field(
        default=256,
        description=(
            "за паузу на брейкпоинте клиент копит события и шлёт их одним "
            "polling-POST; дефолтных 16 пакетов не хватает"
        ),
    )

    auth_secret: str | None = Field(
        default=None,
        description=(
            "Секрет подписи JWT; chainlit читает его только из env "
            "(CHAINLIT_AUTH_SECRET), бутстрап прокидывает значение туда."
        ),
    )

    ws_per_message_deflate: bool = Field(
        default=True,
        description="Сжатие WebSocket-фреймов (permessage-deflate) в uvicorn.",
    )

    ws_protocol: str = Field(
        default="auto",
        description="WebSocket-реализация uvicorn: auto/websockets/wsproto/none.",
    )


class PostgresPoolConfig(BaseModel):
    """Параметры конструктора psycopg_pool.AsyncConnectionPool (без callable-хуков)."""

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
    """libpq 'options': серверные GUC сессии (-c key=value); сериализуется в строку."""

    model_config = ConfigDict(extra="ignore")

    search_path: str | None = Field(
        default=None, description="search_path сессии: схема или список 'a,b'."
    )
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

    def to_options(self) -> str | None:
        """Собирает libpq options: '-c k=v -c k2=v2'; None — если ничего не задано."""
        parts = [
            f"-c {name}={value}"
            for name in type(self).model_fields
            if (value := getattr(self, name)) is not None
        ]
        return " ".join(parts) or None

    @property
    def primary_schema(self) -> str | None:
        """Первая схема из search_path — её создаёт провайдер (CREATE SCHEMA)."""
        if not self.search_path:
            return None
        return self.search_path.split(",")[0].strip().strip('"')


class PostgresConfig(BaseModel):
    """libpq connection keywords + поведение connect() psycopg; см. PostgreSQL docs."""

    model_config = ConfigDict(extra="ignore")

    # libpq connection параметры
    # https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNSTRING
    host: str | None = Field(default=None, description="Хост(ы) или путь к сокету.")
    hostaddr: str | None = Field(default=None, description="IP хоста (без DNS).")
    port: int | None = Field(default=None, description="Порт (или сокет-суффикс).")
    dbname: str | None = Field(default=None, description="Имя БД.")
    user: str | None = Field(default=None, description="Пользователь.")
    password: str | None = Field(default=None, description="Пароль (секрет).")
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
        default=0,
        description="Порог prepared statements; 0 — отключить (нужно для pgbouncer).",
    )

    # серверные опции сессии (libpq 'options'); сериализуются в строку в to_pg_conn,
    # search_path здесь — единый источник схемы (провайдер по нему создаёт схему)
    options: Annotated[
        PostgresOptionsConfig,
        Field(
            default_factory=lambda: PostgresOptionsConfig.model_validate({}),
            description="Серверные GUC сессии (search_path/timeouts) → libpq options.",
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

    def to_pg_conn(self) -> dict:
        """kwargs для connect(): libpq-ключи + autocommit/prepare_threshold + opts."""
        # pool/options — не скалярные connect-параметры: pool это конструктор пула,
        # options сериализуется отдельно в строку '-c k=v'
        out = {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name not in ("pool", "options")
        }
        # незаданные параметры опускаем — libpq возьмёт дефолт/env
        conn = {k: v for k, v in out.items() if v is not None}
        if opts := self.options.to_options():
            conn["options"] = opts
        return conn

    def to_pg_pool(self) -> dict:
        """kwargs конструктора AsyncConnectionPool (без None)."""
        return {
            name: value
            for name in type(self.pool).model_fields
            if (value := getattr(self.pool, name)) is not None
        }


class CredentialsAuthConfig(BaseModel):
    """Авторизация по статической таблице логин/пароль из конфига."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["credentials"] = "credentials"

    users: dict[str, str] = Field(
        default_factory=lambda: {"admin": "admin"},
        description="Таблица логин→пароль; совпадение выдаёт роль admin.",
    )


class KerberosDelegationConfig(BaseModel):
    """
    Куда класть ccache и продлевать ли токен
    """

    model_config = ConfigDict(extra="ignore")

    ccache_template: str = Field(
        default="MEMORY:agent-{principal}",
        description="Шаблон имени ccache на пользователя {principal} подставляется",
    )
    renew: bool = Field(
        default=True,
        description="Продлевать renewable-тикет по запросу при ошибке истечения.",
    )


class KerberosAuthConfig(BaseModel):
    """SSO через Kerberos/SPNEGO: тикет валидирует middleware, роль — из групп AD."""

    type: Literal["kerberos"] = "kerberos"

    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    bind_dn: str = Field(description="DN сервисной учётки для поиска пользователя.")
    bind_password: str = Field(description="Пароль сервисной учётки (секрет).")
    user_filter: str = Field(
        default="(sAMAccountName={username})",
        description="LDAP-фильтр поиска пользователя; {username} подставляется.",
    )
    group_role_map: dict[str, str] = Field(
        default_factory=dict,
        description="DN группы → роль приложения; берётся первая совпавшая по порядку.",
    )
    service_name: str = Field(
        description="SPN сервиса (HTTP/host@REALM), явно — без автоподбора из keytab.",
    )
    keytab: str = Field(
        description=(
            "Путь к keytab сервиса (ключ SPN для SPNEGO-accept); "
            "обычно /etc/krb5.keytab."
        ),
    )
    header: str = Field(
        default="X-Remote-User",
        description="Заголовок, куда кладётся принципал для header_auth_callback.",
    )
    delegation: KerberosDelegationConfig = Field(
        default_factory=KerberosDelegationConfig,
        description="Параметры ccache для unconstrained режима делегирования",
    )


class LdapAuthConfig(BaseModel):
    """Логин/пароль с проверкой bind'ом в AD; роль — из групп AD (как kerberos)."""

    type: Literal["ldap"] = "ldap"

    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    user_filter: str = Field(
        default="(sAMAccountName={username})",
        description="LDAP-фильтр поиска пользователя; {username} подставляется.",
    )
    bind_dn_template: str = Field(
        description="LDAP bind user; {username} подставляется",
    )
    group_role_map: dict[str, str] = Field(
        default_factory=dict,
        description="DN группы → роль приложения; берётся первая совпавшая по порядку.",
    )



AuthConfig = Annotated[
    CredentialsAuthConfig | KerberosAuthConfig | LdapAuthConfig,
    Field(discriminator="type"),
]


class AppConfig(BaseModel):
    """Параметры chainlit-приложения: server + профиль агента."""

    model_config = ConfigDict(extra="ignore")

    chainlit: Annotated[
        ChainlitExtendConfig,
        Field(
            description=("Chainlit config; ${chainlit.<name>}."),
        ),
    ]

    agent: Annotated[
        AgentProfile,
        Field(
            description=(
                "Профиль агента; в конфиге подключается ссылкой ${agent.<name>}."
            ),
        ),
    ]

    auth: Annotated[
        AuthConfig,
        Field(
            default_factory=CredentialsAuthConfig,
            description="Способ авторизации: credentials|kerberos (по полю type).",
        ),
    ]

    logger: Annotated[
        dict,
        Field(
            default=LOGGING_CONFIG,
            description="Конфигурация логера",
        ),
    ]

    checkpoints: Annotated[
        PostgresConfig,
        Field(description=("Конфигурация postgres для сохранения данных agent")),
    ]
