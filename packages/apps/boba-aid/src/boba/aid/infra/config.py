"""Схема конфигурации приложения: pydantic-модели секций toml-конфига.

Ошибки: своих не выпускает; выбор профиля — boba.chat.profiles.
"""

from typing import Annotated, Any, ClassVar, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    ConfigDict,
    Field,
    HttpUrl,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from boba.aid.agent.config import AgentSettings, ProcessAgentConfig
from boba.aid.auth.config import SessionConfig
from boba.aid.infra.messaging_backends import StandardMessagingBackends
from boba.aid.mcp_servers.config import McpServersConfig
from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig, ProxyAuthConfig
from boba.auth.providers.backends import StandardAuthBackends
from boba.auth.settings import SignInSettings
from boba.chat.profiles import LocalChatProfileConfig, ProfileConfig
from boba.config import ConfigVariant
from boba.config.paths import PathsConfig, TlsConfig
from boba.db.postgres.connection import PostgresConfig
from boba.http_server import ServerConfig
from boba.messaging.config import ClusterConfig, MessagingConfig

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


class AidSection(ServerConfig):
    """Секция [server]: HTTP-сервер чата — адрес, TLS, префикс приложения,
    пульс сокетов, остановка uvicorn и песочница приложений MCP Apps. Модель
    собирает AppConfig; bootstrap поднимает по ней uvicorn и ставит префикс,
    AidListeners открывает её сокеты. Статику chainlit точка входа берёт из
    share/ каталога программы, вложения — из files/ каталога данных
    экземпляра."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    port: int = Field(default=8501, gt=0, lt=65536)
    ssl_cert: str | None = Field(default=None)
    ssl_key: str | None = Field(default=None)
    ssl_ca_certs: str | None = Field(default=None)

    sandbox_port: int = Field(
        gt=0,
        le=65535,
        description=(
            "Порт страницы-прокси песочницы приложений MCP Apps: одна на "
            "процесс, открывается при старте. Адрес её страницы в браузере — "
            "схема и хост public_url с этим портом."
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

    def sandbox_url(self) -> str:
        """Адрес страницы-прокси песочницы в браузере: схема и хост страницы
        чата, порт песочницы, корень пути."""
        public = HttpUrl(self.public_url)
        host = public.host
        if host is None:
            msg = f"[server].public_url {self.public_url} names no host"
            raise ValueError(msg)

        sandbox = HttpUrl.build(scheme=public.scheme, host=host, port=self.sandbox_port)

        return str(sandbox)

    def sandbox_origin(self) -> str:
        """Источник страницы-прокси песочницы в форме браузера."""
        return self._origin(HttpUrl(self.sandbox_url()))

    def chat_origin(self) -> str:
        """Источник страницы чата в форме браузера: единственный, кому
        разрешено встраивать песочницу."""
        return self._origin(HttpUrl(self.public_url))

    @staticmethod
    def _origin(url: HttpUrl) -> str:
        """Схема, хост и порт, если он не порт схемы по умолчанию; учётные
        данные адреса в источник не входят."""
        host = url.host
        if host is None:
            msg = f"[server]: address {url} expects a host, got none"
            raise ValueError(msg)

        bare = HttpUrl.build(scheme=url.scheme, host=host, port=url.port)
        parts = urlsplit(str(bare))

        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


class AppConfig(SignInSettings):
    """Секции процесса aid поверх общих: [server], профили, роли и агент."""

    db_schema: str = Field(alias="schema", min_length=1)
    paths: PathsConfig
    tls: TlsConfig = Field(default_factory=TlsConfig)
    session: SessionConfig
    postgres: PostgresConfig
    cluster: ClusterConfig
    messaging: SerializeAsAny[MessagingConfig]

    AUTH_BACKENDS: ClassVar[AuthBackends] = StandardAuthBackends().registry
    MESSAGING_BACKENDS: ClassVar = StandardMessagingBackends().registry
    CONFIG_VARIANTS: ClassVar = {
        AuthConfig: ConfigVariant("type", AUTH_BACKENDS.model_for),
        MessagingConfig: ConfigVariant("provider", MESSAGING_BACKENDS.model_for),
    }

    @field_validator("messaging", mode="before")
    @classmethod
    def _messaging_backend(cls, raw: object) -> MessagingConfig:
        return cls.MESSAGING_BACKENDS.parse(raw)

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    server: Annotated[
        AidSection,
        Field(description="Секция [server]: HTTP-сервер чата; ${server.<name>}."),
    ]

    mcp_servers: McpServersConfig

    profiles: Annotated[
        dict[str, ProfileConfig],
        Field(
            min_length=1,
            description=(
                "Профили чата по имени; в конфиге подключается ссылкой ${profiles}."
            ),
        ),
    ]

    @model_validator(mode="after")
    def _local_profiles_have_models(self) -> Self:
        if isinstance(self.agent, ProcessAgentConfig):
            for name, profile in self.profiles.items():
                if not isinstance(profile, LocalChatProfileConfig):
                    raise ValueError(
                        f"[profiles.{name}]: a local agent requires a model"
                    )
        return self

    @model_validator(mode="after")
    def _proxy_realms_are_isolated(self) -> Self:
        routes: set[str] = set()
        secrets: set[str] = set()
        for proxy in self.auth_entries(ProxyAuthConfig):
            if proxy.path in routes:
                msg = (
                    f"[auth]: proxy realm {proxy.identity.realm!r} repeats route "
                    f"{proxy.path!r}; configure a separate route for each realm"
                )
                raise ValueError(msg)

            secret = proxy.secret.get_secret_value()
            if secret in secrets:
                msg = (
                    "[auth]: HTTP proxy realms require distinct signing secrets "
                    "so a signed request cannot be replayed in another realm"
                )
                raise ValueError(msg)

            routes.add(proxy.path)
            secrets.add(secret)

        return self

    @model_validator(mode="after")
    def _sandbox_off_the_chat(self) -> Self:
        """Песочница приложений не на источнике чата и не на его порту:
        иначе скрипт приложения достаёт до страницы чата, а сокет песочницы
        не поднимется рядом с сокетом чата."""
        if self.server.sandbox_port == self.server.port:
            msg = (
                f"[server].sandbox_port {self.server.sandbox_port} is the port "
                f"the chat itself listens on ([server].port {self.server.port}); "
                "give the app "
                "sandbox a port of its own"
            )
            raise ValueError(msg)

        if self.server.sandbox_origin() != self.server.chat_origin():
            return self

        msg = (
            f"[server].sandbox_port {self.server.sandbox_port} puts the app sandbox on "
            f"origin {self.server.sandbox_origin()} of the chat page itself "
            f"(public_url {self.server.public_url}); give the sandbox a port "
            "other than "
            "the port of the chat in the browser, so that the apps cannot reach "
            "the chat"
        )
        raise ValueError(msg)

    auth: Annotated[
        list[SerializeAsAny[AuthConfig]],
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
        AgentSettings,
        Field(description="Секция [agent]: процесс-агент ACP на вкладку треда."),
    ]
