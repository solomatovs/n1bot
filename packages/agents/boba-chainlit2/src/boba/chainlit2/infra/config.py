from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.chat.auth.fix import FixAuthConfig
from boba.chainlit2.chat.auth.kerberos import KerberosAuthConfig
from boba.chainlit2.chat.auth.ldap import LdapAuthConfig

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


class ChainlitExtendConfig(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8501)
    ssl_cert: str | None = Field(default=None)
    ssl_key: str | None = Field(default=None)
    ssl_ca_certs: str | None = Field(default=None)
    url_prefix: str = Field(default="")
    root: str = Field(default="")

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
    """
    libpq 'options': серверные GUC сессии (-c key=value); сериализуется в строку.
    """

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

    def to_options(self, override_options: dict[str, str] | None = None) -> str | None:
        """libpq options '-c k=v ...': таймауты + опц. search_path; None если пусто."""
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

    def conn_settings(self, override_options: dict[str, str] | None = None) -> dict:
        """
        kwargs для connect(): libpq-ключи + autocommit/prepare_threshold + opts.
        """
        # pool/options — не скалярные connect-параметры: pool это конструктор пула,
        # options сериализуется отдельно в строку '-c k=v'
        conn = {}

        for name in type(self).model_fields:
            if name not in ("pool", "options"):
                value = getattr(self, name)
                if value is not None:
                    conn[name] = value

        if opts := self.options.to_options(override_options):
            conn["options"] = opts

        return conn

    def pool_settings(self) -> dict:
        """kwargs конструктора AsyncConnectionPool (без None)."""
        res = {}
        for name in type(self.pool).model_fields:
            if (value := getattr(self.pool, name)) is not None:
                res[name] = value
        return res


class CheckpointerConfig(BaseModel):
    """Конфиг langgraph-checkpointer: схема БД (через search_path)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema: str = Field(
        default="public",
        alias="schema",
        description=(
            "Схема для таблиц checkpointer. Задаётся в search_path соединения, "
            "т.к. AsyncPostgresSaver пишет имена таблиц без схемы."
        ),
    )


class DataLayerConfig(BaseModel):
    """Конфиг chainlit data layer: схема БД (квалифицируется в SQL явно) + лимиты."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema: str = Field(
        default="public",
        alias="schema",
        description="Схема таблиц data layer; PostgresDataLayer квалифицирует ею SQL.",
    )


class LocalStorageConfig(BaseModel):
    """Локальное файловое хранилище вложений (реализация BaseStorageClient)."""

    model_config = ConfigDict(extra="ignore")

    files_dir: str = Field(
        description=(
            "Корневая папка на диске для файлов вложений (<files_dir>/<object_key>)."
        )
    )
    public_prefix: str = Field(
        default="/upload",
        description="URL-префикс serve-роута; из него собирается url элемента.",
    )


AuthConfig = Annotated[
    FixAuthConfig | KerberosAuthConfig | LdapAuthConfig,
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
        list[AuthConfig],
        Field(
            default_factory=list,
            description="Доступные способы авторизации",
        ),
    ]

    logger: Annotated[
        dict,
        Field(
            default=LOGGING_CONFIG,
            description="Конфигурация логера",
        ),
    ]

    postgres: Annotated[
        PostgresConfig,
        Field(
            description="Общий postgres: подключение + пул (data layer и checkpointer)."
        ),
    ]

    checkpointer: Annotated[
        CheckpointerConfig,
        Field(
            default_factory=lambda: CheckpointerConfig.model_validate({}),
            description="Схема langgraph-checkpointer.",
        ),
    ]

    data_layer: Annotated[
        DataLayerConfig,
        Field(
            default_factory=lambda: DataLayerConfig.model_validate({}),
            description="Схема и лимиты chainlit data layer.",
        ),
    ]

    storage: Annotated[
        LocalStorageConfig,
        Field(description="Файловое хранилище вложений."),
    ]
