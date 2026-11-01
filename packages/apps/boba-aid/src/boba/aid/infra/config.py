"""Схема конфигурации приложения: pydantic-модели секций toml-конфига.

Ошибки: своих не выпускает; выбор профиля — boba.chat.profiles.
"""

from typing import Annotated, Any, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    model_validator,
)

from boba.access import RoleConfig
from boba.aid.agent.config import AgentConfig
from boba.aid.mcp_servers.config import McpUserConfig
from boba.auth.config import AuthConfig
from boba.chat.profiles import ChatProfileConfig
from boba.runtime.config import RuntimeConfig

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


class AidSection(BaseModel):
    """Секция [aid]: HTTP-сервер чата — адрес, TLS, префикс приложения,
    пульс сокетов, остановка uvicorn и песочница приложений MCP Apps. Модель
    собирает AppConfig; bootstrap поднимает по ней uvicorn и ставит префикс,
    AidListeners открывает её сокеты. Статику chainlit точка входа берёт из
    share/ каталога программы, вложения — из files/ каталога данных
    экземпляра."""

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8501)
    ssl_cert: str | None = Field(default=None)
    ssl_key: str | None = Field(default=None)
    ssl_ca_certs: str | None = Field(default=None)
    url_prefix: str = Field(default="")

    chat_url: HttpUrl = Field(
        description=(
            "Адрес страницы чата в браузере пользователя: схема, хост, порт и "
            "префикс, ровно как его открывают. Только его источник может "
            "встроить страницу песочницы приложений MCP Apps."
        ),
    )

    sandbox_port: int = Field(
        gt=0,
        le=65535,
        description=(
            "Порт страницы-прокси песочницы приложений MCP Apps: одна на "
            "процесс, открывается при старте. Адрес её страницы в браузере — "
            "схема и хост chat_url с этим портом."
        ),
    )

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


    def sandbox_url(self) -> str:
        """Адрес страницы-прокси песочницы в браузере: схема и хост страницы
        чата, порт песочницы, корень пути."""
        host = self.chat_url.host
        if host is None:
            msg = f"[aid].chat_url {self.chat_url} names no host"
            raise ValueError(msg)

        sandbox = HttpUrl.build(
            scheme=self.chat_url.scheme, host=host, port=self.sandbox_port
        )

        return str(sandbox)

    def sandbox_origin(self) -> str:
        """Источник страницы-прокси песочницы в форме браузера."""
        return self._origin(HttpUrl(self.sandbox_url()))

    def chat_origin(self) -> str:
        """Источник страницы чата в форме браузера: единственный, кому
        разрешено встраивать песочницу."""
        return self._origin(self.chat_url)

    @staticmethod
    def _origin(url: HttpUrl) -> str:
        """Схема, хост и порт, если он не порт схемы по умолчанию; учётные
        данные адреса в источник не входят."""
        host = url.host
        if host is None:
            msg = f"[aid]: address {url} expects a host, got none"
            raise ValueError(msg)

        bare = HttpUrl.build(scheme=url.scheme, host=host, port=url.port)
        parts = urlsplit(str(bare))

        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


class AppConfig(RuntimeConfig):
    """Секции процесса aid поверх общих: [aid], профили, роли и агент."""

    model_config = ConfigDict(extra="ignore")

    aid: Annotated[
        AidSection,
        Field(description="Секция [aid]: HTTP-сервер чата; ${aid.<name>}."),
    ]

    mcp_user: McpUserConfig = Field(default_factory=McpUserConfig)

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

    @model_validator(mode="after")
    def _sandbox_off_the_chat(self) -> Self:
        """Песочница приложений не на источнике чата и не на его порту:
        иначе скрипт приложения достаёт до страницы чата, а сокет песочницы
        не поднимется рядом с сокетом чата."""
        if self.aid.sandbox_port == self.aid.port:
            msg = (
                f"[aid].sandbox_port {self.aid.sandbox_port} is the port the chat "
                f"itself listens on ([aid].port {self.aid.port}); give the app "
                "sandbox a port of its own"
            )
            raise ValueError(msg)

        if self.aid.sandbox_origin() != self.aid.chat_origin():
            return self

        msg = (
            f"[aid].sandbox_port {self.aid.sandbox_port} puts the app sandbox on "
            f"origin {self.aid.sandbox_origin()} of the chat page itself "
            f"(chat_url {self.aid.chat_url}); give the sandbox a port other than "
            "the port of the chat in the browser, so that the apps cannot reach "
            "the chat"
        )
        raise ValueError(msg)

    auth: Annotated[
        list[AuthConfig],
        Field(
            default_factory=list,
            description="Доступные способы авторизации",
        ),
    ]

    logger: dict[str, Any] = Field(
        default=LOGGING_CONFIG,
        description="Конфигурация логера: с полем user из сессии chainlit.",
    )

    agent: Annotated[
        AgentConfig,
        Field(description="Секция [agent]: процесс-агент ACP на вкладку треда."),
    ]
