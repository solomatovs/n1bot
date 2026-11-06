"""Корневая конфигурация AID: состав секций и проверки их согласованности."""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Self

from pydantic import (
    ConfigDict,
    Field,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from boba.aid.config.models import (
    AgentSettings,
    AidSection,
    DataConfig,
    McpServersConfig,
    ProcessAgentConfig,
    SessionConfig,
    TlsConfig,
)
from boba.aid.infra.messaging_backends import StandardMessagingBackends
from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig
from boba.auth.providers.backends import StandardAuthBackends
from boba.auth.settings import SignInSettings
from boba.chat.profiles import LocalChatProfileConfig, ProfileConfig
from boba.config import ConfigVariant
from boba.db.postgres.connection import PostgresConfig
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


class AppConfig(SignInSettings):
    """Секции процесса aid поверх общих: [server], профили, роли и агент."""

    db_schema: str = Field(alias="schema", min_length=1)
    paths: DataConfig = Field(alias="data")
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
