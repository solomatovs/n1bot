"""Общие секции конфига приложений: каталоги kerberos, профили, роли, вход, данные, api.

Ошибки:
RuntimeError — конфиг ещё не загружен (RawConfig.get до RawConfig.load).
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.access import RoleConfig
from boba.auth.config import (
    AuthConfig,
    KerberosAuthConfig,
)
from boba.chat.profiles import ChatProfileConfig
from boba.config import ConfigBuilder, bind
from boba.db.postgres.profile import PostgresConfig
from boba.identity.token import SessionRenewal
from boba.krb import KerberosWorkspaceConfig
from boba.krb.seal import SsoTickets, TicketSealer
from boba.runtime.launchers import ToolLaunchers

__all__ = [
    "AppLayers",
    "AppName",
    "BuiltPage",
    "ClusterConfig",
    "ConfigLocator",
    "DataLayerConfig",
    "DevPage",
    "EnvOverride",
    "LocalMessagingConfig",
    "MessagingConfig",
    "PageSource",
    "PostgresMessagingConfig",
    "ProcessLogging",
    "RawConfig",
    "RuntimeConfig",
    "SessionConfig",
    "StreamJournalConfig",
    "StudioConfig",
    "StudioPath",
    "StudioRuntimeConfig",
]


class ConfigLocator:
    """Путь конфига тестового стенда: BOBA_CONFIG_PATH либо BOBA_BASE/conf/config.toml.

    Приложения получают конфиг обязательным аргументом запуска; локатор остаётся
    только фикстурам и стендам, которые аргументов не имеют.
    """

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


class EnvOverride(StrEnum):
    """Ключи секции [env], которые переменная окружения BOBA_* переопределяет.

    Значение члена — имя ключа в [env]; имя переменной складывается из имени
    члена: BASE -> BOBA_BASE. Всё остальное задаётся только конфигом.
    """

    BASE = "base"
    DATA = "data"
    PORT = "port"
    INSTANCE_ID = "instance_id"
    HOST = "host"
    URL_PREFIX = "url_prefix"
    CGROUP_BASE = "cgroup_base"
    APP_ROOT = "app_root"
    WORKFLOW_PAGE = "workflow_page"
    CATALOG_PAGE = "catalog_page"
    MESSAGING = "messaging_provider"
    TOOL_LAUNCHER = "tool_launcher"

    @property
    def var(self) -> str:
        return f"BOBA_{self.name}"


class AppLayers:
    """Слои конфига процесса: вычисленный base -> toml -> плагины -> BOBA_-оверрайды.

    Конфиг самодостаточен: значения секции [env] описаны в toml, base выводится
    из раскладки (config.toml лежит в ${base}/conf), файлы conf/plugins/<id>.toml
    ложатся секциями tool.<id>, а окружение лишь переопределяет ключи из
    реестра EnvOverride.
    """

    HOST_FALLBACK: ClassVar[str] = "HOSTNAME"
    PLUGINS_DIR: ClassVar[str] = "plugins"
    PLUGIN_SUFFIX: ClassVar[str] = ".toml"

    @classmethod
    def compose(cls, config_path: Path) -> DictConfig:
        builder = ConfigBuilder()
        builder.add_dict(cls._computed(config_path))
        builder.add_toml(config_path)
        builder.add_dict(cls._plugins(config_path))
        builder.add_dict(cls._overrides())

        return builder.build()

    @classmethod
    def _computed(cls, config_path: Path) -> dict[str, Any]:
        base = config_path.resolve().parent.parent
        return {"env": {"base": str(base)}}

    @classmethod
    def _plugins(cls, config_path: Path) -> dict[str, Any]:
        """Файлы conf/plugins/<id>.toml -> секции tool.<id>; интерполяции файлов
        резолвятся от корня собранного конфига.
        """
        plugins_dir = config_path.parent / cls.PLUGINS_DIR
        if not plugins_dir.is_dir():
            return {}

        sections: dict[str, Any] = {}
        for path in sorted(plugins_dir.glob(f"*{cls.PLUGIN_SUFFIX}")):
            with path.open("rb") as body:
                sections[path.stem] = tomllib.load(body)

        if not sections:
            return {}

        return {"tool": sections}

    @classmethod
    def _overrides(cls) -> dict[str, Any]:
        entries: dict[str, str] = {}
        for override in EnvOverride:
            value = cls._value_of(override)
            if value is None:
                continue

            entries[override.value] = value

        if not entries:
            return {}

        return {"env": entries}

    @classmethod
    def _value_of(cls, override: EnvOverride) -> str | None:
        value = os.environ.get(override.var)
        if value is not None:
            return value

        # имя узла в кластере по умолчанию берётся у контейнера: один конфиг
        # обслуживает несколько узлов тест-стенда
        if override is EnvOverride.HOST:
            return os.environ.get(cls.HOST_FALLBACK)

        return None


class RawConfig:
    """Загруженный toml приложения: один на процесс, провайдеры читают его секциями."""

    _raw: ClassVar[DictConfig | None] = None

    @classmethod
    def load(cls, config_path: Path) -> DictConfig:
        cls._raw = AppLayers.compose(config_path)

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


class AppName(StrEnum):
    """Приложения над сервисами; значение служит суффиксом имени инстанса и колонкой
    app в live_instances.
    """

    CHAINLIT = "chainlit"
    STUDIO = "studio"


class ClusterConfig(BaseModel):
    """Секция [cluster]: имя узла, из которого с именем приложения складывается имя
    инстанса, и сроки жизни блокировок, слушателя шины и хранимых событий.
    """

    model_config = ConfigDict(extra="forbid")

    SEPARATOR: ClassVar[str] = "-"

    node_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    host: str = Field(min_length=1, description="Узел, где лежат журналы инструментов.")
    lock_ttl_sec: int = Field(
        gt=0, description="Срок блокировки без подтверждения жизни."
    )
    heartbeat_sec: int = Field(
        gt=0, description="Период подтверждения жизни держателем."
    )
    reaper_period_sec: int = Field(
        gt=0, description="Период сторожа протухших блокировок."
    )
    queue_usage_limit: float = Field(
        gt=0,
        le=1,
        description="Доля очереди NOTIFY, при которой слушатель переподключается.",
    )
    retention_sec: int = Field(
        gt=0, description="Сколько хранить события и тела областей, в которых тихо."
    )

    @model_validator(mode="after")
    def _heartbeat_fits_ttl(self) -> Self:
        if self.heartbeat_sec * 2 > self.lock_ttl_sec:
            msg = "cluster: heartbeat_sec must be at most half of lock_ttl_sec"
            raise ValueError(msg)

        return self

    def instance_of(self, app: AppName) -> str:
        return f"{self.node_id}{self.SEPARATOR}{app.value}"


class LocalMessagingConfig(BaseModel):
    """Шина сообщений в памяти процесса: один инстанс, доставка внутри publish.

    Лишние ключи игнорируются: секция общая для всех провайдеров, provider
    выбирается env-переменной.
    """

    model_config = ConfigDict(extra="ignore")

    provider: Literal["local"]


class PostgresMessagingConfig(BaseModel):
    """Шина сообщений в Postgres: доставка между инстансами через LISTEN/NOTIFY."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    provider: Literal["postgres"]

    postgres: PostgresConfig = Field(
        description="Подключение и пул; в конфиге подключается ссылкой ${postgres}.",
    )

    db_schema: str = Field(
        min_length=1,
        alias="schema",
        description="Схема таблиц шины live_*: общая для всех приложений кластера.",
    )


MessagingConfig = Annotated[
    LocalMessagingConfig | PostgresMessagingConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


class StudioPath(StrEnum):
    """Что студия вешает под url_prefix: api, его socket.io и страница."""

    API = "/api"
    SOCKET = "/socket.io"
    PAGE = "/workflow"


class SessionConfig(BaseModel):
    """Секция [session]: JWT и cookie входа, общие для обоих приложений — токен одного
    принимает другое.
    """

    model_config = ConfigDict(extra="forbid")

    auth_secret: str = Field(
        min_length=1, description="Секрет JWT входа: подпись и печать билета."
    )
    cookie: str = Field(min_length=1, description="Имя cookie входа.")
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        description="SameSite cookie входа; none включает Secure."
    )
    session_ttl_sec: int = Field(gt=0, description="Срок JWT и cookie входа.")
    session_max_sec: int = Field(
        gt=0,
        description=(
            "Потолок сессии от первого входа: дольше без нового входа не продлить."
        ),
    )

    @model_validator(mode="after")
    def _max_covers_ttl(self) -> SessionConfig:
        if self.session_max_sec < self.session_ttl_sec:
            msg = "session_max_sec must not be shorter than session_ttl_sec"
            raise ValueError(msg)

        return self

    def renewal(self) -> SessionRenewal:
        return SessionRenewal.of(self.session_ttl_sec, self.session_max_sec)


class StudioConfig(BaseModel):
    """Секция [studio]: адрес процесса и источник страницы workflow."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int
    url_prefix: str = Field(
        description="Префикс приложения: api под /api, страница — /workflow."
    )
    ws_protocol: Literal["auto", "websockets", "wsproto", "none"] = Field(
        description=(
            "WebSocket-реализация uvicorn; websockets режет заголовки длиннее 8 КБ, "
            "а cookie входа с билетом SSO больше — нужен wsproto."
        )
    )
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
        return f"{self.url_prefix}{StudioPath.API}"

    def socket_path(self) -> str:
        return f"{self.api_prefix()}{StudioPath.SOCKET}"


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
    stream_journal: StreamJournalConfig
    session: SessionConfig
    cluster: ClusterConfig
    messaging: MessagingConfig

    @classmethod
    def load(cls, config_path: Path) -> Self:
        """Читает toml, проверяет способ запуска инструментов и раскладывает кэши
        kerberos.
        """
        raw = RawConfig.load(config_path)
        config = bind(raw, path=cls.SECTION, model=cls)
        # предпосылки способа запуска проверяются на старте: отказ виден сразу
        ToolLaunchers.of(raw).probe()
        # кэши билетов раскладывает приложение: строкам соединений пути не задают
        config.krb.apply()

        return config

    @field_validator("auth")
    @classmethod
    def _kerberos_at_most_once(cls, value: list[AuthConfig]) -> list[AuthConfig]:
        found = 0
        for entry in value:
            if isinstance(entry, KerberosAuthConfig):
                found += 1

        if found > 1:
            # один SPNEGO-обмен на приложение: второй [auth.kerberos] — ошибка конфига
            msg = "kerberos authorization configured twice"
            raise ValueError(msg)

        return value

    def pg_messaging(self) -> PostgresMessagingConfig:
        """Секция [messaging] postgres-провайдера; при local — RuntimeError."""
        if isinstance(self.messaging, PostgresMessagingConfig):
            return self.messaging

        msg = "[messaging] provider is not postgres"
        raise RuntimeError(msg)

    def kerberos(self) -> KerberosAuthConfig | None:
        for entry in self.auth:
            if isinstance(entry, KerberosAuthConfig):
                return entry

        return None

    def sso_path(self) -> str:
        """Путь SPNEGO-обмена из [auth.kerberos]; без него — RuntimeError."""
        kerberos = self.kerberos()
        if kerberos is None:
            msg = "sso is configured without [auth.kerberos]"
            raise RuntimeError(msg)

        return kerberos.sso_path

    def sso_tickets(self) -> SsoTickets | None:
        """Открыватель билетов SSO-входа; None — kerberos в [auth] не настроен."""
        kerberos = self.kerberos()
        if kerberos is None:
            return None

        return SsoTickets(
            sealer=TicketSealer(self.session.auth_secret),
            krb5_config=kerberos.delegation.krb5_config,
        )


class StudioRuntimeConfig(RuntimeConfig):
    """Конфиг процесса studio: общие секции плюс [studio]."""

    model_config = ConfigDict(extra="ignore")

    studio: StudioConfig
