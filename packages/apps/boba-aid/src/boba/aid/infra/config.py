"""Схема конфигурации приложения: pydantic-модели секций toml-конфига.

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
from boba.aid.agent.config import AgentConfig
from boba.auth.config import AuthConfig
from boba.chat.profiles import ChatProfileConfig
from boba.mcp_client.client import McpServersConfig
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
    пульс сокетов и остановка uvicorn. Модель собирает AppConfig; bootstrap
    поднимает по ней uvicorn и ставит префикс. Статику chainlit точка входа
    берёт из share/ каталога программы, вложения — из files/ каталога данных
    экземпляра."""

    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8501)
    ssl_cert: str | None = Field(default=None)
    ssl_key: str | None = Field(default=None)
    ssl_ca_certs: str | None = Field(default=None)
    url_prefix: str = Field(default="")

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


class AppConfig(RuntimeConfig):
    """Секции процесса aid поверх общих: [aid], профили, MCP-серверы, агент."""

    model_config = ConfigDict(extra="ignore")

    aid: Annotated[
        AidSection,
        Field(description="Секция [aid]: HTTP-сервер чата; ${aid.<name>}."),
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

    mcp: Annotated[
        McpServersConfig,
        Field(description="Секция [mcp]: MCP-серверы, чьи инструменты видит модель."),
    ]

    @model_validator(mode="after")
    def _profiles_name_declared_servers(self) -> Self:
        """Профиль называет только серверы из [mcp.servers]: опечатка в имени
        — отказ на старте, а не профиль без инструментов."""
        declared = sorted(self.mcp.servers)
        for name, profile in self.profiles.items():
            for server in profile.mcp:
                if server in self.mcp.servers:
                    continue

                msg = (
                    f"[profiles.{name}].mcp names server {server!r} that is not "
                    f"declared in [mcp.servers], declared: {declared}"
                )
                raise ValueError(msg)

        return self

    @model_validator(mode="after")
    def _sandboxes_off_the_chat(self) -> Self:
        """Песочница приложений сервера не на источнике чата и не на его
        порту: иначе скрипт приложения достаёт до страницы чата, а сокет
        песочницы не поднимется рядом с сокетом чата."""
        for name, server in self.mcp.servers.items():
            apps = server.apps
            if apps is None:
                continue

            if apps.port == self.aid.port:
                msg = (
                    f"[mcp.servers.{name}.apps] port {apps.port} is the port the "
                    f"chat itself listens on ([aid].port {self.aid.port}); give the "
                    "sandbox of the server a port of its own"
                )
                raise ValueError(msg)

            if apps.origin() != apps.chat_origin():
                continue

            msg = (
                f"[mcp.servers.{name}.apps] port {apps.port} puts the sandbox on "
                f"origin {apps.origin()} of the chat page itself (chat_url "
                f"{apps.chat_url}); give the sandbox a port other than the port "
                "of the chat in the browser, so that the apps cannot reach the chat"
            )
            raise ValueError(msg)

        return self

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
