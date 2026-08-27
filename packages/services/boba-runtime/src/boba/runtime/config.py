"""Общие секции конфига приложений: каталоги kerberos, профили, роли, вход, данные, api.

Ошибки:
RuntimeError — конфиг ещё не загружен (RawConfig.get до RawConfig.load).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.access import RoleConfig
from boba.chat.profiles import ChatProfileConfig
from boba.connections.postgres import PostgresConfig
from boba.krb import KerberosWorkspaceConfig
from boba.krb.seal import SsoTickets, TicketSealer
from boba.runtime.auth_config import AuthConfig, KerberosAuthConfig
from boba.sandbox import CgroupManager
from boba.sandbox.profile import SandboxConfig
from boba.settings import bind, build_app_config

__all__ = [
    "BuiltPage",
    "ConfigLocator",
    "DataLayerConfig",
    "DevPage",
    "PageSource",
    "ProcessLogging",
    "RawConfig",
    "RuntimeConfig",
    "StudioConfig",
]


class ConfigLocator:
    """Путь конфига процесса: BOBA_CONFIG_PATH либо BOBA_BASE/conf/config.toml."""

    CONFIG_ENV: ClassVar[str] = "BOBA_CONFIG_PATH"
    BASE_ENV: ClassVar[str] = "BOBA_BASE"
    CONFIG_RELATIVE: ClassVar[str] = "conf/config.toml"

    @classmethod
    def path(cls) -> Path:
        if config_path := os.environ.get(cls.CONFIG_ENV):
            return Path(config_path)

        base = os.environ.get(cls.BASE_ENV)
        if not base:
            msg = f"{cls.CONFIG_ENV} or {cls.BASE_ENV} is required"
            raise RuntimeError(msg)

        return Path(base) / cls.CONFIG_RELATIVE


class RawConfig:
    """Загруженный toml приложения: один на процесс, провайдеры читают его секциями."""

    _raw: ClassVar[DictConfig | None] = None

    @classmethod
    def load(cls, config_path: Path) -> DictConfig:
        cls._raw = build_app_config(config_path=config_path)

        return cls._raw

    @classmethod
    def get(cls) -> DictConfig:
        if cls._raw is None:
            msg = "raw config is not loaded: call RawConfig.load() first"
            raise RuntimeError(msg)

        return cls._raw


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


class BuiltPage(BaseModel):
    """Страница workflow отдаётся из сборки в public/workflow."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["built"] = "built"


class DevPage(BaseModel):
    """Страница workflow проксируется с vite dev-сервера по адресу url."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["dev"] = "dev"
    url: str = Field(pattern=r"^https?://[^/]+$", description="Адрес vite без пути.")


class PageSource:
    """Разбор значения [workflow] page: 'built' либо адрес vite dev-сервера."""

    BUILT: ClassVar[str] = "built"

    @classmethod
    def parse(cls, raw: object) -> object:
        if not isinstance(raw, str):
            return raw

        if raw == cls.BUILT:
            return BuiltPage()

        return DevPage(url=raw.rstrip("/"))


class StudioConfig(BaseModel):
    """Секция [studio]: адрес процесса, секрет JWT входа, источник страницы workflow."""

    model_config = ConfigDict(extra="forbid")

    MOUNT: ClassVar[str] = "/api"
    SOCKET_PATH: ClassVar[str] = "/socket.io"

    host: str
    port: int
    url_prefix: str = Field(
        description="Общий префикс приложений: api под /api, страница — /workflow."
    )
    auth_secret: str = Field(
        min_length=1, description="Секрет JWT chainlit: подпись и печать билета."
    )
    cookie: str = Field(min_length=1, description="Имя cookie входа chainlit.")
    page: BuiltPage | DevPage = Field(
        discriminator="kind",
        description="'built' — сборка из dist; адрес — vite dev-сервер.",
    )
    dist: Path = Field(description="Каталог сборки страницы: index.html и assets/.")

    @field_validator("page", mode="before")
    @classmethod
    def _parse_page(cls, raw: object) -> object:
        return PageSource.parse(raw)

    def api_prefix(self) -> str:
        return f"{self.url_prefix}{self.MOUNT}"

    def socket_path(self) -> str:
        return f"{self.api_prefix()}{self.SOCKET_PATH}"


class ProcessLogging:
    """Логирование процесса по умолчанию: приложение в stderr, access-лог в stdout."""

    @classmethod
    def default(cls) -> dict[str, Any]:
        return {
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
            "root": {"handlers": ["default"], "level": "INFO"},
            "loggers": {
                "uvicorn": {
                    "handlers": ["default"],
                    "level": "INFO",
                    "propagate": False,
                },
                "uvicorn.error": {"level": "INFO"},
                "uvicorn.access": {
                    "handlers": ["access"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
        }


class RuntimeConfig(BaseModel):
    """Секции [app], нужные любому процессу приложения; остальное читает сам процесс."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "app"

    krb: KerberosWorkspaceConfig
    profiles: dict[str, ChatProfileConfig]
    roles: dict[str, RoleConfig]
    auth: list[AuthConfig]
    logger: dict[str, Any] = Field(default_factory=ProcessLogging.default)
    data_layer: DataLayerConfig
    sandbox: SandboxConfig
    studio: StudioConfig

    @classmethod
    def load(cls, config_path: Path) -> Self:
        """Читает toml, проверяет лимиты песочницы и раскладывает кэши kerberos."""
        raw = RawConfig.load(config_path)
        config = bind(raw, path=cls.SECTION, model=cls)
        # групповые лимиты проверяются на старте: отказ виден сразу, с именем профиля
        CgroupManager.probe_profiles(config.sandbox.profiles)
        # кэши билетов раскладывает приложение: строкам соединений пути не задают
        config.krb.apply()

        return config

    def kerberos(self) -> KerberosAuthConfig | None:
        for entry in self.auth:
            if isinstance(entry, KerberosAuthConfig):
                return entry

        return None

    def sso_tickets(self) -> SsoTickets | None:
        """Открыватель билетов SSO-входа; None — kerberos в [auth] не настроен."""
        kerberos = self.kerberos()
        if kerberos is None:
            return None

        return SsoTickets(
            sealer=TicketSealer(self.studio.auth_secret),
            krb5_config=kerberos.delegation.krb5_config,
        )
