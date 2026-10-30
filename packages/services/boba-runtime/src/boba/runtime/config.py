"""Общие секции конфига приложений: каталоги kerberos, профили, роли, вход, данные, api.

Ошибки:
ConfigError — файл конфигурации не читается, не разрешается или секция не
    сходится с моделью.
RuntimeError — конфиг ещё не загружен (RawConfig.get до RawConfig.load).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from omegaconf import DictConfig
from pydantic import (
    Base64Bytes,
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    field_validator,
    model_validator,
)

from boba.access import RoleConfig
from boba.auth.config import (
    AuthConfig,
    KerberosAuthConfig,
    ProxyAuthConfig,
)
from boba.chat.profiles import ChatProfileConfig
from boba.config import ConfigLoader, bind
from boba.db.postgres.connection import PostgresConfig
from boba.identity.token import CookieSpec, SessionRenewal
from boba.krb import KerberosWorkspaceConfig
from boba.krb.seal import SsoTickets, TicketSealer
from boba.workspace.launcher import LauncherEnv, LauncherInterpreter

__all__ = [
    "AppName",
    "ClusterConfig",
    "ConfigArguments",
    "LocalMessagingConfig",
    "MessagingConfig",
    "PostgresMessagingConfig",
    "ProcessConfig",
    "ProcessInterpreter",
    "ProcessLogging",
    "RawConfig",
    "RuntimeConfig",
    "SessionConfig",
    "StreamJournalConfig",
    "TokenConfig",
    "ValueCipherConfig",
]


class ConfigArguments:
    """Аргументы запуска приложения: --config <файл> один или несколько раз.

    Первый файл — конфиг экземпляра; следующие — наложения поверх него
    (стенды и отладка). Значений по умолчанию и поиска «рядом» нет: какие
    файлы читаются, видно из командной строки.
    """

    CONFIG: ClassVar[str] = "--config"

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self._parser = parser

    def paths(self) -> tuple[Path, ...]:
        self._parser.add_argument(
            self.CONFIG,
            required=True,
            action="append",
            type=Path,
            help=(
                "path to config.toml of this instance; repeat to lay an overlay "
                "file over it"
            ),
        )
        arguments = self._parser.parse_args()

        return tuple(arguments.config)


class ProcessInterpreter:
    """Интерпретатор процесса и его окружение для цепочки лаунчера песочницы.

    Это не конфиг, а факт самого процесса: им цепочка лаунчера (LauncherChain,
    зиготы) запускает тот же python во внешнем слое. Снимок делают реестр
    способов запуска инструментов и хранилище образов, когда создаются.
    """

    def snapshot(self) -> LauncherInterpreter:
        """Текущий каталог и его предки в корни не идут: python -m кладёт cwd в
        sys.path, а cwd сервиса — дерево установки с конфигом и данными.
        """
        env: dict[LauncherEnv, str] = {}
        for name in LauncherEnv:
            value = os.environ.get(name.value)
            if not value:
                continue

            env[name] = value

        libraries = env.get(LauncherEnv.LD_LIBRARY_PATH, "").split(os.pathsep)
        # запуск относительным путём оставляет в sys.executable `..`: внутри
        # слоя такой путь не пройти, там нет промежуточных каталогов
        executable = os.path.abspath(sys.executable)
        candidates = (
            sys.base_prefix,
            sys.prefix,
            sys.exec_prefix,
            os.path.dirname(executable),
            os.path.dirname(os.path.realpath(executable)),
            *sys.path,
            *libraries,
        )

        cwd = os.getcwd()
        roots: list[str] = []
        for candidate in candidates:
            if not os.path.isabs(candidate):
                continue

            root = os.path.normpath(candidate)
            if self._holds(root, cwd):
                continue

            roots.append(root)

        return LauncherInterpreter(
            executable=executable, env=env, roots=tuple(dict.fromkeys(roots))
        )

    @staticmethod
    def _holds(root: str, path: str) -> bool:
        """root — сам path или его предок."""
        if path == root:
            return True

        return path.startswith(root.rstrip("/") + "/")


class RawConfig:
    """Загруженное дерево конфига: одно на процесс, провайдеры читают его секциями."""

    _raw: ClassVar[DictConfig | None] = None

    @classmethod
    def load(cls, paths: Sequence[Path]) -> DictConfig:
        cls._raw = ConfigLoader(paths).load()

        return cls._raw

    @classmethod
    def get(cls) -> DictConfig:
        if cls._raw is None:
            msg = (
                "RawConfig.get: raw config is not loaded yet, "
                "RawConfig.load(paths) must run first"
            )
            raise RuntimeError(msg)

        return cls._raw


class StreamJournalConfig(BaseModel):
    """Секция [stream_journal]: журнал живого вывода инструментов, служебный
    том на пользователя. Её читает провайдер журналов процесса, который
    исполняет инструменты (boba-mcp); у чата журналов нет."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "stream_journal"

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

    AID = "aid"
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
    """Шина сообщений в Postgres: доставка между инстансами через LISTEN/NOTIFY.
    Таблицы live_* живут в схеме приложения на пуле процесса."""

    model_config = ConfigDict(extra="ignore")

    provider: Literal["postgres"]


MessagingConfig = Annotated[
    LocalMessagingConfig | PostgresMessagingConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


class ValueCipherConfig(BaseModel):
    """Ключ шифрования секретов в строках таблицы: секция хранилища, которое
    шифрует значения, — [connections] (соединения пользователей). Ключ приходит
    ссылкой `cipher = "${keys.encryption}"` и проверяется здесь, на границе
    конфига; по нему хранилище создаёт SecretCipher."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cipher: Secret[Annotated[Base64Bytes, Field(min_length=32, max_length=32)]] = Field(
        description=(
            "Ключ шифрования значений: 32 байта в base64; смена ключа делает "
            "сохранённые секреты нечитаемыми."
        ),
    )


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
    cookie входа.
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
    """Секции [app], нужные любому процессу, который принимает вход: kerberos,
    способы входа, токен сессии. Базовая для RuntimeConfig (чат) и конфига
    сервиса boba-mcp.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    SECTION: ClassVar[str] = "app"

    db_schema: str = Field(
        alias="schema",
        min_length=1,
        description=(
            "Схема хранения приложения: в ней все его таблицы; плагины берут "
            "её ссылкой ${app.schema}."
        ),
    )
    krb: KerberosWorkspaceConfig
    auth: list[AuthConfig]
    logger: dict[str, Any] = Field(default_factory=ProcessLogging.default)
    session: TokenConfig

    @classmethod
    def load(cls, paths: Sequence[Path]) -> Self:
        """Читает файлы конфигурации и раскладывает кэши kerberos."""
        return cls.of(RawConfig.load(paths))

    @classmethod
    def of(cls, raw: DictConfig) -> Self:
        """Секция [app] уже загруженного дерева; раскладывает кэши kerberos."""
        config = bind(raw, path=cls.SECTION, model=cls)
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
    подключение процесса, профили и роли чата, браузерная часть сессии, шина
    сообщений и узел кластера."""

    postgres: PostgresConfig = Field(
        description=(
            "Подключение процесса ссылкой ${postgres}: по нему открывается "
            "единственный пул, его получают все таблицы приложения."
        ),
    )
    connections: ValueCipherConfig = Field(
        description="Секция [connections]: шифрование соединений пользователей."
    )
    profiles: dict[str, ChatProfileConfig]
    roles: dict[str, RoleConfig]
    session: SessionConfig
    cluster: ClusterConfig
    messaging: MessagingConfig
