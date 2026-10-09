"""Общие секции конфига приложений: каталоги kerberos, профили, роли, вход, данные, api.

Ошибки:
ConfigLayoutError — файлы конфигурации названы или разложены не так: нет файла,
    не заданы переменные стенда, в общем конфиге есть секция [env] или [site],
    в site-файле есть секция сверх этих двух.
RuntimeError — конфиг ещё не загружен (RawConfig.get до RawConfig.load).
"""

from __future__ import annotations

import argparse
import os
import secrets
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from boba.access import RoleConfig
from boba.auth.config import (
    AuthConfig,
    KerberosAuthConfig,
    ProxyAuthConfig,
)
from boba.chat.profiles import ChatProfileConfig
from boba.config import ConfigBuilder, bind
from boba.db.postgres.connection import PostgresConfig
from boba.identity.token import CookieSpec, SessionRenewal
from boba.krb import KerberosWorkspaceConfig
from boba.krb.seal import SsoTickets, TicketSealer
from boba.runtime.launchers import ToolLaunchers

__all__ = [
    "AppLayers",
    "AppName",
    "BuiltPage",
    "ClusterConfig",
    "ConfigArguments",
    "ConfigFiles",
    "ConfigLayoutError",
    "ConfigLocator",
    "DataLayerConfig",
    "DevPage",
    "EnvOverride",
    "LocalMessagingConfig",
    "MessagingConfig",
    "PageSource",
    "PostgresMessagingConfig",
    "ProcessConfig",
    "ProcessLogging",
    "RawConfig",
    "RuntimeConfig",
    "SessionConfig",
    "StreamJournalConfig",
    "StudioConfig",
    "StudioPath",
    "StudioRuntimeConfig",
    "TokenConfig",
]


class ConfigLayoutError(Exception):
    """Файлы конфигурации названы или разложены не так, как ждёт загрузчик."""


@dataclass(frozen=True)
class ConfigFiles:
    """Два файла конфигурации процесса.

    config — общий конфиг приложения: устройство приложения, одинаковое на любой
    машине, лежит в пакете и в git. site — специфика случая запуска: пути, адреса,
    секреты; свой у compose, отладки и установки, в git не входит. Каталог над
    conf/, в котором лежит site, становится env.base: данные случая лежат рядом
    со спецификой.
    """

    config: Path
    site: Path

    def __post_init__(self) -> None:
        for role, path in (("config", self.config), ("site", self.site)):
            if not path.is_file():
                msg = f"config files: {role} expects an existing toml file, got {path}"
                raise ConfigLayoutError(msg)

    def base(self) -> Path:
        """Корень раскладки случая: каталог над conf/ с site-файлом."""
        return self.site.resolve().parent.parent

    def plugins_dir(self) -> Path:
        """Файлы плагинов лежат рядом с общим конфигом."""
        return self.config.parent / "plugins"


class ConfigArguments:
    """Аргументы запуска приложения: --config <общий конфиг> и --site <специфика>.

    Оба обязательны, значений по умолчанию и поиска «рядом» нет: какой файл
    читается, видно из командной строки.
    """

    CONFIG: ClassVar[str] = "--config"
    SITE: ClassVar[str] = "--site"

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self._parser = parser

    def files(self) -> ConfigFiles:
        self._parser.add_argument(
            self.CONFIG,
            required=True,
            type=Path,
            help="path to the application config.toml (common, from the package)",
        )
        self._parser.add_argument(
            self.SITE,
            required=True,
            type=Path,
            help="path to site.toml (paths, addresses and secrets of this run)",
        )
        arguments = self._parser.parse_args()

        return ConfigFiles(config=arguments.config, site=arguments.site)


class ConfigLocator:
    """Файлы конфигурации тестового стенда: BOBA_CONFIG_PATH и BOBA_SITE_PATH.

    Приложения получают оба пути обязательными аргументами запуска; локатор
    остаётся только фикстурам и стендам, которые аргументов не имеют.
    """

    CONFIG_ENV: ClassVar[str] = "BOBA_CONFIG_PATH"
    SITE_ENV: ClassVar[str] = "BOBA_SITE_PATH"

    @classmethod
    def files(cls) -> ConfigFiles:
        config = os.environ.get(cls.CONFIG_ENV)
        site = os.environ.get(cls.SITE_ENV)
        if not config or not site:
            msg = (
                f"config files: {cls.CONFIG_ENV} and {cls.SITE_ENV} are both "
                f"required in the environment, got {cls.CONFIG_ENV}={config!r}, "
                f"{cls.SITE_ENV}={site!r}"
            )
            raise ConfigLayoutError(msg)

        return ConfigFiles(config=Path(config), site=Path(site))


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
    MESSAGING = "messaging_provider"
    TOOL_LAUNCHER = "tool_launcher"
    MCP_HOST = "mcp_host"
    MCP_PORT = "mcp_port"
    MCP_SCHEME = "mcp_scheme"
    MCP_PREFIX = "mcp_prefix"
    PUBLIC_URL = "public_url"

    @property
    def var(self) -> str:
        return f"BOBA_{self.name}"


class AppLayers:
    """Слои конфига процесса: вычисленный base -> общий конфиг -> плагины ->
    site-файл -> BOBA_-оверрайды.

    Общий конфиг описывает устройство приложения и на [env] и [site] только
    ссылается; сами секции лежат в site-файле, и другой секции в нём быть не
    может — иначе устройство приложения описывалось бы в двух местах. base
    выводится из раскладки случая (site.toml лежит в ${base}/conf), файлы
    plugins/<id>.toml рядом с общим конфигом ложатся секциями tool.<id>, а
    окружение лишь переопределяет ключи [env] из реестра EnvOverride.
    """

    HOST_FALLBACK: ClassVar[str] = "HOSTNAME"
    PLUGIN_SUFFIX: ClassVar[str] = ".toml"
    SITE_SECTIONS: ClassVar[frozenset[str]] = frozenset({"env", "site"})

    @classmethod
    def compose(cls, files: ConfigFiles) -> DictConfig:
        builder = ConfigBuilder()
        builder.add_dict({"env": {"base": str(files.base())}})
        builder.add_dict(cls._common(files.config))
        builder.add_dict(cls._plugins(files.plugins_dir()))
        builder.add_dict(cls._site(files.site))
        builder.add_dict(cls._overrides())

        return builder.build()

    @classmethod
    def _common(cls, path: Path) -> dict[str, Any]:
        with path.open("rb") as body:
            sections = tomllib.load(body)

        misplaced = sorted(cls.SITE_SECTIONS & set(sections))
        if misplaced:
            msg = (
                f"common config {path}: sections {misplaced} belong to the site "
                "file; the common config only refers to them as ${env.*} and "
                "${site.*}"
            )
            raise ConfigLayoutError(msg)

        return sections

    @classmethod
    def _site(cls, path: Path) -> dict[str, Any]:
        with path.open("rb") as body:
            sections = tomllib.load(body)

        foreign = sorted(set(sections) - cls.SITE_SECTIONS)
        if foreign:
            msg = (
                f"site file {path}: expected only sections "
                f"{sorted(cls.SITE_SECTIONS)}, got also {foreign}; the structure "
                "of the application is described in the common config alone"
            )
            raise ConfigLayoutError(msg)

        return sections

    @classmethod
    def _plugins(cls, plugins_dir: Path) -> dict[str, Any]:
        """Файлы plugins/<id>.toml -> секции tool.<id>; интерполяции файлов
        резолвятся от корня собранного конфига.
        """
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
    def load(cls, files: ConfigFiles) -> DictConfig:
        cls._raw = AppLayers.compose(files)

        return cls._raw

    @classmethod
    def get(cls) -> DictConfig:
        if cls._raw is None:
            msg = (
                "RawConfig.get: raw config is not loaded yet, "
                "RawConfig.load(files) must run first"
            )
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
            msg = (
                "[stream_journal]: dir is required when enable = true, "
                "got an empty string"
            )
            raise ValueError(msg)

        return self


class AppName(StrEnum):
    """Приложения над сервисами; значение служит суффиксом имени инстанса и колонкой
    app в live_instances.
    """

    CHAINLIT = "chainlit"
    STUDIO = "studio"
    MCP = "mcp"


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
            msg = (
                "[cluster]: heartbeat_sec must be at most half of lock_ttl_sec, "
                f"got heartbeat_sec = {self.heartbeat_sec} and "
                f"lock_ttl_sec = {self.lock_ttl_sec}"
            )
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


class TokenConfig(BaseModel):
    """Секция [session] любого процесса со входом: секрет подписи, срок
    токена, потолок сессии и поколение сессий. Базовая для SessionConfig;
    сама описывает [session] сервиса boba-mcp, который выдаёт токены OAuth,
    а не cookie.
    """

    model_config = ConfigDict(extra="forbid")

    auth_secret: str = Field(
        min_length=1, description="Секрет JWT входа: подпись и печать билета."
    )
    session_ttl_sec: int = Field(gt=0, description="Срок JWT входа.")
    session_max_sec: int = Field(
        gt=0,
        description=(
            "Потолок сессии от первого входа: дольше без нового входа не продлить."
        ),
    )
    generation: str = Field(
        default="",
        description=(
            "Поколение сессий: токены другого поколения отвергаются. Пусто — "
            "случайное на каждый старт процесса, и рестарт разлогинивает всех; "
            "задано — общее для приложений, которые должны принимать токены "
            "друг друга."
        ),
    )

    @model_validator(mode="after")
    def _max_covers_ttl(self) -> Self:
        if self.session_max_sec < self.session_ttl_sec:
            msg = (
                "[session]: session_max_sec must not be shorter than "
                f"session_ttl_sec, got session_max_sec = {self.session_max_sec} "
                f"and session_ttl_sec = {self.session_ttl_sec}"
            )
            raise ValueError(msg)

        return self

    _process_generation: ClassVar[str] = ""
    """Случайное поколение процесса: одно на все читатели токенов в нём."""

    def session_generation(self) -> str:
        """Поколение из конфига либо случайное, общее для процесса."""
        if self.generation:
            return self.generation

        if not TokenConfig._process_generation:
            TokenConfig._process_generation = secrets.token_hex(16)

        return TokenConfig._process_generation

    def renewal(self) -> SessionRenewal:
        return SessionRenewal.of(self.session_ttl_sec, self.session_max_sec)


class SessionConfig(TokenConfig):
    """Секция [session] приложения с браузером: к токену сессии добавлена
    cookie входа. Общая для чата и studio — токен одного принимает другое.
    """

    cookie: str = Field(min_length=1, description="Имя cookie входа.")
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        description="SameSite cookie входа; none включает Secure."
    )

    def cookie_spec(self) -> CookieSpec:
        return CookieSpec(
            name=self.cookie,
            samesite=self.cookie_samesite,
            ttl_sec=self.session_ttl_sec,
        )


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

    def page_prefix(self) -> str:
        """Полный адрес страницы studio: {prefix}/workflow."""
        return f"{self.url_prefix}{StudioPath.PAGE}"


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


class ProcessConfig(BaseModel):
    """Секции [app], нужные любому процессу, который исполняет инструменты и
    принимает вход: kerberos, способы входа, токен сессии, журналы вызовов.
    Базовая для RuntimeConfig (чат, studio) и конфига сервиса boba-mcp.
    """

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "app"

    krb: KerberosWorkspaceConfig
    auth: list[AuthConfig]
    logger: dict[str, Any] = Field(default_factory=ProcessLogging.default)
    stream_journal: StreamJournalConfig
    session: TokenConfig

    @classmethod
    def load(cls, files: ConfigFiles) -> Self:
        """Читает файлы конфигурации, проверяет способ запуска инструментов и
        раскладывает кэши kerberos.
        """
        raw = RawConfig.load(files)
        config = bind(raw, path=cls.SECTION, model=cls)
        # предпосылки способа запуска проверяются на старте: отказ виден сразу
        ToolLaunchers(raw).build().probe()
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
            msg = (
                "[auth]: expected at most one kerberos entry, "
                f"got {found} entries with kind = kerberos"
            )
            raise ValueError(msg)

        return value

    def kerberos(self) -> KerberosAuthConfig | None:
        for entry in self.auth:
            if isinstance(entry, KerberosAuthConfig):
                return entry

        return None

    def proxy(self) -> ProxyAuthConfig | None:
        for entry in self.auth:
            if isinstance(entry, ProxyAuthConfig):
                return entry

        return None

    def sso_path(self) -> str:
        """Путь SPNEGO-обмена из [auth.kerberos]; без него — RuntimeError."""
        kerberos = self.kerberos()
        if kerberos is None:
            msg = (
                "sso path: [auth] has no entry with kind = kerberos, "
                "so no SPNEGO exchange path exists"
            )
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


class RuntimeConfig(ProcessConfig):
    """Секции [app] приложения с браузером: к секциям процесса добавлены
    профили и роли чата, data layer, браузерная часть сессии, шина сообщений
    и узел кластера."""

    profiles: dict[str, ChatProfileConfig]
    roles: dict[str, RoleConfig]
    data_layer: DataLayerConfig
    session: SessionConfig
    cluster: ClusterConfig
    messaging: MessagingConfig

    def pg_messaging(self) -> PostgresMessagingConfig:
        """Секция [messaging] postgres-провайдера; при local — RuntimeError."""
        if isinstance(self.messaging, PostgresMessagingConfig):
            return self.messaging

        msg = (
            "[messaging]: a postgres provider is required here, "
            f"got provider = {self.messaging.provider!r}"
        )
        raise RuntimeError(msg)


class StudioRuntimeConfig(RuntimeConfig):
    """Конфиг процесса studio: общие секции плюс [studio]."""

    model_config = ConfigDict(extra="ignore")

    studio: StudioConfig
