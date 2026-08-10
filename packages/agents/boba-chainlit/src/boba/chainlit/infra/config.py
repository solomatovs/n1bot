"""Схема конфигурации приложения: pydantic-модели секций toml-конфига."""

import os
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.chainlit.auth import AuthConfig
from boba.db.postgres import PostgresConfig
from boba.sandbox.profile import SandboxConfig
from boba.workspace.launcher import LauncherConfig

LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s [%(user)s] %(message)s",
            "use_colors": True,
        },
        "uvcorn": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": True,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s [%(user)s] %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
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


class OpenAiDumpConfig(BaseModel):
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
            msg = "openai.dump: enable = true требует path"
            raise ValueError(msg)

        return self


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

    dump: OpenAiDumpConfig = Field(
        default_factory=OpenAiDumpConfig,
        description="Дамп HTTP-обмена с провайдером.",
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

    history_messages: int = Field(
        default=30,
        ge=1,
        description=(
            "Сколько последних сообщений истории уходит в LLM. Считаются "
            "только реплики: вызовы инструментов и их результаты из прошлых "
            "ходов вырезаются, текущий ход передаётся целиком."
        ),
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


class CheckpointerConfig(BaseModel):
    """Конфиг langgraph-checkpointer: postgres-подключение + схема БД."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    postgres: Annotated[
        PostgresConfig,
        Field(
            description=(
                "Подключение и пул; в конфиге подключается ссылкой ${postgres}."
            ),
        ),
    ]

    db_schema: str = Field(
        default="public",
        alias="schema",
        description=(
            "Схема для таблиц checkpointer. Задаётся в search_path соединения, "
            "т.к. AsyncPostgresSaver пишет имена таблиц без схемы."
        ),
    )


class DataLayerConfig(BaseModel):
    """Конфиг chainlit data layer: postgres-подключение + схема БД."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    postgres: Annotated[
        PostgresConfig,
        Field(
            description=(
                "Подключение и пул; в конфиге подключается ссылкой ${postgres}."
            ),
        ),
    ]

    db_schema: str = Field(
        default="public",
        alias="schema",
        description="Схема таблиц data layer; PostgresDataLayer квалифицирует ею SQL.",
    )


class LocalStorageConfig(BaseModel):
    """Хранилище вложений (реализация BaseStorageClient)."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["local", "image"] = Field(
        default="local",
        description="local — файлы на диске; image — внутри per-thread ext4-образа.",
    )
    files_dir: str = Field(
        default="",
        description=(
            "Корневая папка на диске для файлов вложений "
            "(<files_dir>/<object_key>); обязательна при kind=local."
        ),
    )
    public_prefix: str = Field(
        default="/upload",
        description="URL-префикс serve-роута; из него собирается url элемента.",
    )
    image_path: str = Field(
        default="",
        description=(
            "kind=image: шаблон пути образа с {user_id}/{thread_id}, "
            'например ".../workspace/{user_id}/{thread_id}.ext4".'
        ),
    )
    image_template: str = Field(
        default="",
        description="kind=image: шаблонный ext4-образ для первого обращения.",
    )
    op_timeout_sec: int = Field(
        default=60,
        ge=1,
        description="kind=image: таймаут одной операции с образом, сек.",
    )
    launcher: LauncherConfig = Field(
        description="kind=image: тайминги и размеры операций лаунчера образов.",
    )

    @field_validator("image_path", "image_template", mode="after")
    @classmethod
    def _canonicalize(cls, value: str) -> str:
        """bwrap не примет относительный путь: корень песочницы read-only."""
        if not value:
            return value
        return os.path.normpath(os.path.abspath(os.path.expanduser(value)))

    @model_validator(mode="after")
    def _validate_kind(self) -> Self:
        if self.kind == "local" and not self.files_dir:
            msg = "storage: kind=local requires files_dir"
            raise ValueError(msg)
        if self.kind == "image" and not (self.image_path and self.image_template):
            msg = "storage: kind=image requires image_path and image_template"
            raise ValueError(msg)
        return self


class StreamJournalConfig(BaseModel):
    """Журнал живого вывода инструментов: служебный том на пользователя."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Писать вывод каждого вызова инструмента в журнал.",
    )

    dir: str = Field(
        default="",
        description=(
            "Корень журналов: каталог, том на пользователя внутри; "
            "переполнение держит отдельная точка монтирования под корнем."
        ),
    )

    reserve_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        description=(
            "Резерв места перед новым журналом: старейшие треды вытесняются, "
            "пока свободного меньше; 0 выключает ротацию."
        ),
    )

    @model_validator(mode="after")
    def _validate_enabled(self) -> Self:
        if not self.enable:
            return self

        if not self.dir:
            msg = "stream_journal: dir is required"
            raise ValueError(msg)

        return self


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

    checkpointer: Annotated[
        CheckpointerConfig,
        Field(description="Сервис langgraph-checkpointer: подключение + схема."),
    ]

    data_layer: Annotated[
        DataLayerConfig,
        Field(description="Сервис chainlit data layer: подключение + схема."),
    ]

    storage: Annotated[
        LocalStorageConfig,
        Field(description="Файловое хранилище вложений."),
    ]

    stream_journal: Annotated[
        StreamJournalConfig,
        Field(
            default_factory=StreamJournalConfig,
            description="Журнал живого вывода инструментов.",
        ),
    ]

    sandbox: Annotated[
        SandboxConfig,
        Field(description="Реестр профилей песочницы; ${sandbox}."),
    ]
