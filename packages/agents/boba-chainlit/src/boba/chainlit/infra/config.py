"""Схема конфигурации приложения: pydantic-модели секций toml-конфига.

Ошибки:
Ошибки: своих не выпускает; выбор профиля — boba.chat.profiles.
"""

from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from boba.access import RoleConfig
from boba.chainlit.auth import AuthConfig
from boba.chainlit.domain.config import LocalStorageConfig
from boba.chat.profiles import (
    ChatProfileConfig,
    SettingsBounds,
)
from boba.connections.postgres import PostgresConfig
from boba.krb import KerberosWorkspaceConfig
from boba.sandbox.profile import SandboxConfig

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
            "Период heartbeat'а engine.io, секунды: с ним сервер шлёт клиенту "
            "ping, значение уезжает клиенту в handshake. Клиент считает "
            "соединение мёртвым, если ping не пришёл за ping_interval + "
            "ping_timeout, и переподключается; большое значение даёт пережить "
            "паузу на breakpoint'е отладки."
        ),
    )

    ping_timeout: int = Field(
        default=300,
        description=(
            "Ожидание pong'а engine.io, секунды: без ответа сервер закрывает "
            "сессию сокета с причиной ping timeout. Вместе с ping_interval "
            "задаёт, через сколько тишины вкладка уходит в реконнект."
        ),
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

    ws_ping_interval: float = Field(
        default=20,
        ge=0,
        description=(
            "Период ws-пинга uvicorn, секунды: кадр ping самого протокола "
            "WebSocket, отдельный от heartbeat'а engine.io. Отвечает на него "
            "браузер, а не приложение. 0 — не пинговать, живость остаётся "
            "только на engine.io."
        ),
    )

    ws_ping_timeout: float = Field(
        default=20,
        ge=0,
        description=(
            "Ожидание ws-pong'а, секунды: без ответа uvicorn рвёт соединение "
            "и вкладка уходит в реконнект, даже когда ход жив. Малое значение "
            "рвёт связь на замершей вкладке и сетевых задержках. 0 — не ждать."
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

    shutdown_timeout_sec: int = Field(
        gt=0,
        description=(
            "Сколько секунд uvicorn ждёт завершения соединений и задач после "
            "сигнала остановки, прежде чем отменить их и выйти. Без этого "
            "значения ожидание бесконечно: открытая вкладка держит websocket, "
            "остановка приложения не доходит до shutdown, а вместе с "
            "приложением остаются жить и зиготы песочницы."
        ),
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
    """Параметры chainlit-приложения: server, профили чата и права ролей."""

    model_config = ConfigDict(extra="ignore")

    krb: Annotated[
        KerberosWorkspaceConfig,
        Field(description="Секция [krb]: krb5.conf и каталог кэшей билетов."),
    ]
    chainlit: Annotated[
        ChainlitExtendConfig,
        Field(
            description=("Chainlit config; ${chainlit.<name>}."),
        ),
    ]

    profiles: Annotated[
        dict[str, ChatProfileConfig],
        Field(
            min_length=1,
            description=(
                "Профили чата по имени; в конфиге подключается ссылкой ${profiles}."
            ),
        ),
    ]

    roles: Annotated[
        dict[str, RoleConfig],
        Field(
            description=(
                "Права ролей по имени; в конфиге подключается ссылкой ${roles}."
            ),
        ),
    ]

    settings: Annotated[
        SettingsBounds,
        Field(
            description=("Пределы пользовательских настроек LLM; ссылкой ${settings}."),
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
