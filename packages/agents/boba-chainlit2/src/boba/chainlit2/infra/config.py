from dataclasses import dataclass
from typing import Annotated, Any

from chainlit.config import ChainlitConfig
from pydantic import BaseModel, ConfigDict, Field

from boba.agent.tool_config import (
    bind,
    build_app_config,
)

LOGGING_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
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
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ProfileConfig(BaseModel):
    """Профиль агента (копия agent.* из конфига): LLM-провайдер + модель + workspace."""

    model_config = ConfigDict(extra="ignore")

    model: Annotated[
        str,
        Field(description="Имя LLM-модели у выбранного провайдера."),
    ]

    openai: Annotated[
        OpenAiConfig,
        Field(
            description=(
                "Транспорт openai-провайдера; в конфиге подключается ссылкой "
                "${openai.<name>}."
            ),
        ),
    ]

    system_prompt_dir: Annotated[
        str,
        Field(description="Корневая директория .md/.txt-файлов с system-prompt."),
    ]

    log_level: str = Field(
        default="INFO",
        description="Уровень корневого логгера.",
    )

    log_file: str | None = Field(
        default=None,
        description="Путь к log-файлу. Пусто — логи в stderr.",
    )

    user_workspace_dir: str = Field(
        default="./workspaces/user",
        description="Корневая директория user-workspace'а.",
    )

    system_workspace_dir: str = Field(
        default="./workspaces/system",
        description="Корневая директория system-workspace'а.",
    )

    stream: bool = Field(
        default=True,
        description=(
            "Режим ответа LLM: True — стриминг дельт, False — один итоговый "
            "ответ без дельт."
        ),
    )

    max_messages: int = Field(
        default=50,
        ge=1,
        description="Размер скользящего окна диалога.",
    )


@dataclass(frozen=True)
class AppConfig(BaseModel):
    """Параметры chainlit-приложения: server + профиль агента."""

    model_config = ConfigDict(extra="ignore")

    # engine.io heartbeat: даём клиенту пережить долгие паузы на брейкпоинтах
    ping_interval: int = Field(
        default=300,
        description="",
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

    temperature: float = Field(
        default=0,
        description="",
    )

    max_tokens: int = Field(
        default=500,
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

    chainlit_config: Annotated[
        ChainlitConfig,
        Field(
            description=("Chainlit config; ${chainlit_config.<name>}."),
        ),
    ]

    profile: Annotated[
        ProfileConfig,
        Field(
            description=(
                "Профиль агента; в конфиге подключается ссылкой ${agent.<name>}."
            ),
        ),
    ]

    logger_config: Annotated[dict, Field(default=LOGGING_CONFIG, description="")]


def get_app_config() -> AppConfig:
    return bind(build_app_config(), path="chainlit2", model=AppConfig)
