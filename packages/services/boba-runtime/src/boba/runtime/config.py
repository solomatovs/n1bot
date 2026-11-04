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
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, TypeVar

from omegaconf import DictConfig
from pydantic import (
    Base64Bytes,
    BaseModel,
    ConfigDict,
    Field,
    Secret,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from boba.auth.assembly import AuthBackends
from boba.auth.config import AuthConfig
from boba.chat.profiles import ProfileConfig
from boba.config import ConfigError, ConfigLoader, ConfigModel, ConfigSchema, bind
from boba.core.components import Components
from boba.db.postgres.connection import PostgresConfig
from boba.identity.service import PublicAddress
from boba.identity.token import CookieSpec, SessionRenewal
from boba.krb import KerberosWorkspaceConfig
from boba.runtime.launchers import SectionLaunchers
from boba.runtime.program import ProgramTool
from boba.sandbox import WorkspaceSpec
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import LauncherChain, LauncherEnv, LauncherInterpreter

__all__ = [
    "AppName",
    "ClusterConfig",
    "InstalledProgram",
    "InstanceConf",
    "InstanceData",
    "InstanceLayout",
    "MessagingConfig",
    "PathsConfig",
    "ProcessConfig",
    "ProcessLogging",
    "ProgramDir",
    "ProgramFile",
    "ProgramRoot",
    "ProgramRoots",
    "RawConfig",
    "RuntimeConfig",
    "ServerConfig",
    "SessionConfig",
    "SourceProgram",
    "SourceTree",
    "StreamJournalConfig",
    "TlsConfig",
    "TokenConfig",
    "ValueCipherConfig",
]


@dataclass(frozen=True)
class MigrationOptions:
    """Выбор SQL-версии приложения и места выполнения всех его планов."""

    version: str | None = None
    database: str | None = None
    schema: str | None = None
    data: Path | None = None

    def __post_init__(self) -> None:
        for value in (self.version, self.database, self.schema):
            if value is not None:
                if not value.strip():
                    raise ValueError(
                        "migration version, database and schema must not be empty"
                    )


class MigrationArguments:
    """Одинаковые флаги команды migrate у AID, Boba и MCP."""

    def add(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--migration-data",
            type=Path,
            help="JSON input available to the SQL scripts",
        )
        parser.add_argument(
            "--migration-version", help="application SQL directory; latest by default"
        )
        parser.add_argument(
            "--database",
            help="override the database for the application and all plugins",
        )
        parser.add_argument(
            "--schema", help="override the schema for the application and all plugins"
        )

    def read(self, arguments: argparse.Namespace) -> MigrationOptions:
        return MigrationOptions(
            arguments.migration_version,
            arguments.database,
            arguments.schema,
            arguments.migration_data,
        )


class ProgramDir(StrEnum):
    """Части каталога программы: их программа находит сама, ключей-путей в
    конфиге экземпляра для них нет."""

    LIBEXEC = "libexec"
    """Вспомогательные программы: bwrap, fuse2fs, mke2fs (mcp)."""
    SHARE = "share"
    """Статика: корень приложения chainlit (aid)."""
    SANDBOX = "sandbox"
    """Шаблон workspace.ext4.gz (mcp)."""


class ProgramFile(StrEnum):
    """Файлы каталога программы относительно его корня."""

    WORKSPACE = "sandbox/workspace.ext4.gz"


class SourceTree(StrEnum):
    """Дерево исходников вокруг runtime/: по нему программа из .venv берёт то,
    что лежит в git, прямо из исходников."""

    PACKAGES = "packages"
    """Каталог пакетов: его наличие рядом с runtime/ и есть дерево исходников."""


@dataclass(frozen=True)
class ProgramRoot(ABC):
    """Каталог программы: только чтение, обновление — замена каталога.

    Базовый класс; наследники — установленная программа (InstalledProgram) и
    программа из исходников (SourceProgram), выбирает их ProgramRoots на
    границе конфига. Пользуются им способ запуска плагинов в песочнице (пути
    из конфигов плагинов, bwrap), хранилище образов workspace и вход чата
    (статика chainlit). root — каталог программы (у исходников — runtime/),
    interpreter — чем цепочка лаунчера песочницы запускает ту же программу во
    внешнем слое.
    """

    root: Path
    interpreter: LauncherInterpreter

    SYSTEM_BINARIES: ClassVar[tuple[str, ...]] = (
        "/usr/bin",
        "/usr/sbin",
        "/bin",
        "/sbin",
    )
    """Системные каталоги после libexec: из них берутся fusermount."""

    WORKSPACE_TARGET: ClassVar[str] = "/workspace"

    @abstractmethod
    def path(self, part: ProgramDir) -> Path:
        """Каталог части программы."""

    def located(self, path: str) -> Path:
        """Путь из конфига: абсолютный — как есть, относительный — от
        каталога программы. Так один конфиг плагина ведёт в образе в
        /app/boba, а из исходников — в runtime/."""
        given = Path(path)
        if given.is_absolute():
            return given

        return self.root / given

    def workspace_template(self) -> Path:
        return self.root / ProgramFile.WORKSPACE.value

    def binaries(self) -> TrustedBinaries:
        """Доверенные каталоги bwrap и fuse2fs: libexec программы, затем
        системные."""
        dirs = (str(self.path(ProgramDir.LIBEXEC)), *self.SYSTEM_BINARIES)

        return TrustedBinaries(dirs=dirs)

    def workspace(self, images: Path) -> WorkspaceSpec:
        """Workspace пользователя: шаблон программы, образ в каталоге images
        экземпляра, точка /workspace внутри песочницы."""
        mount = f"{images}/{WorkspaceSpec.USER_VAR}.ext4:{self.WORKSPACE_TARGET}"

        return WorkspaceSpec.model_validate(
            {"template": str(self.workspace_template()), "mount": mount}
        )


@dataclass(frozen=True)
class InstalledProgram(ProgramRoot):
    """Реализация ProgramRoot собранной Nuitka программы в образе и дереве
    релиза. Все части — в её каталоге."""

    def path(self, part: ProgramDir) -> Path:
        return self.root / part.value


@dataclass(frozen=True)
class SourceProgram(ProgramRoot):
    """Реализация ProgramRoot программы из исходников (.venv): то, что лежит в
    git, берётся по путям приложения относительно sources. Остальные части
    берутся из root — каталога собранных ресурсов."""

    sources: Path
    paths: Mapping[ProgramDir, Path] = field(default_factory=dict)

    def path(self, part: ProgramDir) -> Path:
        if source := self.paths.get(part):
            return self.sources / source

        return self.root / part.value


class ProgramRoots:
    """Поиск каталога программы на границе конфига: единственное место, где
    различаются собранная программа и программа из исходников.

    Собранная программа (Nuitka, `__compiled__`) лежит в
    <корень>/bin/<имя>.dist/<имя>.bin, и корень — третий предок бинарника;
    лаунчер песочницы и проба cgroup — её подкоманды (ProgramTool).
    Программа на python бывает только из исходников: каталог runtime/
    репозитория называет переменная BOBA_RUNTIME, и рядом с ним обязано
    лежать дерево исходников (каталог packages). Создают его компоненты,
    которым каталог программы нужен: способ запуска плагинов, хранилище
    образов, вход чата.
    """

    RUNTIME_ENV: ClassVar[str] = "BOBA_RUNTIME"
    BINARY_DEPTH: ClassVar[int] = 2

    SOURCES_PROBE: ClassVar[tuple[str, ...]] = ("-c", "")
    """Пустой процесс интерпретатора исходников для пробы cgroup."""

    def __init__(self, source_paths: Mapping[ProgramDir, Path] | None = None) -> None:
        self._source_paths: dict[ProgramDir, Path] = {}
        if source_paths is not None:
            self._source_paths.update(source_paths)

    def compiled(self) -> bool:
        """Процесс — собранная программа: интерпретатора python у неё нет."""
        return "__compiled__" in globals()

    def locate(self) -> ProgramRoot:
        if self.compiled():
            return self._compiled()

        return self._python()

    def _compiled(self) -> ProgramRoot:
        binary = Path(sys.executable).resolve()
        root = binary.parents[self.BINARY_DEPTH]
        executable = str(binary)
        interpreter = LauncherInterpreter(
            executable=executable,
            env={},
            roots=(str(root),),
            launcher=(executable, ProgramTool.LAUNCHER.value),
            probe=(executable, ProgramTool.PROBE.value),
        )

        return InstalledProgram(root=root, interpreter=interpreter)

    def _python(self) -> ProgramRoot:
        runtime = os.environ.get(self.RUNTIME_ENV)
        if not runtime:
            msg = (
                f"program directory: the program runs on python from the "
                f"sources, so {self.RUNTIME_ENV} must name runtime/ of the "
                "repository; it is not set"
            )
            raise ConfigError(msg)

        root = Path(runtime).resolve()
        if not root.is_dir():
            msg = (
                f"program directory: {self.RUNTIME_ENV}={runtime} must name an "
                "existing directory laid out as a program, it does not exist"
            )
            raise ConfigError(msg)

        sources = root.parent
        if not (sources / SourceTree.PACKAGES.value).is_dir():
            msg = (
                f"program directory: {self.RUNTIME_ENV}={runtime} must be "
                f"runtime/ of the repository with {SourceTree.PACKAGES.value}/ "
                f"next to it, {sources} has none; an installed program is "
                "compiled and finds its directory by itself"
            )
            raise ConfigError(msg)

        return SourceProgram(
            root=root,
            interpreter=self._interpreter(),
            sources=sources,
            paths=self._source_paths,
        )

    def _interpreter(self) -> LauncherInterpreter:
        """Интерпретатор исходников со своим окружением и корнями.

        Текущий каталог и его предки в корни не идут: python -m кладёт cwd в
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

        # -P: корень слоя не встаёт первым в sys.path лаунчера
        launcher = (executable, "-P", "-m", LauncherChain.MODULE)

        return LauncherInterpreter(
            executable=executable,
            env=env,
            roots=tuple(dict.fromkeys(roots)),
            launcher=launcher,
            probe=(executable, *self.SOURCES_PROBE),
        )

    @staticmethod
    def _holds(root: str, path: str) -> bool:
        """root — сам path или его предок."""
        if path == root:
            return True

        return path.startswith(root.rstrip("/") + "/")


class InstanceData(StrEnum):
    """Фиксированная раскладка каталога данных экземпляра ([paths].data_dir):
    единственного места, куда пишет программа."""

    KRB_CACHE = "krb/cache"
    """Кэши билетов kerberos, в том числе TGT сервиса (krb5cc_http)."""
    DUMP = "dump"
    WORKSPACE = "workspace"
    """Образы workspace пользователей (песочница) или их каталоги (process)."""
    TOOL_LOGS = "tool-logs"
    FILES = "files"
    """Вложения сессий чата."""
    AGENTS = "agents"
    """Приватные каталоги процессов агента (режим process чата)."""


class InstanceConf(StrEnum):
    """Файлы каталога conf/ экземпляра — того, где лежит его config.toml."""

    KRB5_CONF = "krb/krb5.conf"
    """krb5.conf; keytab'ы лежат рядом и называются в конфиге именем файла."""


class TlsConfig(BaseModel):
    """Доверенные CA исходящих подключений процесса и сетевых песочниц."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    SECTION: ClassVar[str] = "tls"
    CA_ENV: ClassVar[str] = "SSL_CERT_FILE"
    ca_file: str = Field(default="/etc/ssl/certs/ca-certificates.crt", min_length=1)

    @field_validator("ca_file")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            raise ValueError("[tls].ca_file must be an absolute path")
        return os.path.normpath(value)


class ServerConfig(PublicAddress):
    """Общие параметры HTTP-сервера приложения."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(gt=0, lt=65536)
    shutdown_timeout_sec: int = Field(default=10, gt=0)


class PathsConfig(BaseModel):
    """Секция [paths]: каталог данных экземпляра и CA-файл контура.

    Раскладка внутри каталога данных фиксирована (InstanceData), а conf/krb
    лежит рядом с config.toml (InstanceConf): других путей экземпляра в
    конфиге нет. Читают её вход процесса (ProcessConfig.of) и компоненты,
    которые пишут в данные: способ запуска плагинов, журналы вызовов,
    хранилище workspace, чат.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SECTION: ClassVar[str] = "paths"

    data_dir: str = Field(
        min_length=1,
        description=(
            "Каталог данных экземпляра: абсолютный путь, раскладка внутри фиксирована."
        ),
    )

    @field_validator("data_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            msg = f"[paths]: expected an absolute path, got {value!r}"
            raise ValueError(msg)

        return os.path.normpath(value)

    def data(self, part: InstanceData) -> Path:
        return Path(self.data_dir) / part.value

    def layout(self, conf_dir: Path, tls: TlsConfig | None = None) -> InstanceLayout:
        return InstanceLayout(conf_dir=conf_dir, paths=self, tls=tls or TlsConfig())


@dataclass(frozen=True)
class InstanceLayout:
    """Каталог экземпляра целиком: conf/ (где лежит config.toml) и данные.

    Собирает его вход процесса из каталога главного файла конфига и секции
    [paths]; apply() ставит процессу kerberos и CA из модели (CLAUDE.md
    §17: в окружение пишет только вход).
    """

    conf_dir: Path
    paths: PathsConfig
    tls: TlsConfig

    def krb5_conf(self) -> Path:
        return self.conf_dir / InstanceConf.KRB5_CONF.value

    def kerberos(self) -> KerberosWorkspaceConfig:
        return KerberosWorkspaceConfig(
            config=str(self.krb5_conf()),
            ccache_dir=str(self.paths.data(InstanceData.KRB_CACHE)),
        )

    def apply(self) -> None:
        """Рабочий каталог kerberos и KRB5_CONFIG, CA-файл контура в
        SSL_CERT_FILE; CA-файла нет — ConfigError."""
        ca_file = Path(self.tls.ca_file)
        if not ca_file.is_file():
            msg = (
                f"[tls]: ca_file must name the CA file of the network "
                f"(PEM), {ca_file} is not a file"
            )
            raise ConfigError(msg)

        self.kerberos().apply()
        os.environ[TlsConfig.CA_ENV] = str(ca_file)


class RawConfig:
    """Загруженное дерево конфига: одно на процесс, провайдеры читают его секциями."""

    _raw: ClassVar[DictConfig | None] = None

    @classmethod
    def load(
        cls, paths: Sequence[Path], *, schema: ConfigSchema | None = None
    ) -> DictConfig:
        cls._raw = ConfigLoader(paths, schema=schema).load()

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
    том на пользователя в tool-logs каталога данных экземпляра. Её читает
    провайдер журналов процесса, который исполняет инструменты (boba-mcp); у
    чата журналов нет."""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "stream_journal"

    enable: bool = Field(
        default=False,
        description="Писать вывод каждого вызова инструмента в журнал.",
    )

    reserve_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        description=(
            "Резерв места перед новым журналом: старейшие треды вытесняются, "
            "пока свободного меньше; 0 выключает ротацию."
        ),
    )


@dataclass(frozen=True)
class AppName:
    """Имя, которое приложение передаёт для регистрации своего инстанса."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("application name must not be empty")

        for char in self.value:
            if char.isascii() and (char.isalnum() or char in "_.-"):
                continue

            raise ValueError(
                f"application name {self.value!r}: expected ASCII letters, "
                "digits, underscores, dots or hyphens"
            )


class ClusterConfig(BaseModel):
    """Секция [cluster]: имя узла, из которого с именем приложения складывается имя
    инстанса, и сроки жизни блокировок, слушателя шины и хранимых событий.
    """

    model_config = ConfigDict(extra="forbid")

    SEPARATOR: ClassVar[str] = "-"

    node_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    host: str = Field(min_length=1, description="Узел, где лежат журналы инструментов.")
    lock_ttl_sec: int = Field(
        default=20, gt=0, description="Срок блокировки без подтверждения жизни."
    )
    heartbeat_sec: int = Field(
        default=6, gt=0, description="Период подтверждения жизни держателем."
    )
    reaper_period_sec: int = Field(
        default=10, gt=0, description="Период сторожа протухших блокировок."
    )
    queue_usage_limit: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="Доля очереди NOTIFY, при которой слушатель переподключается.",
    )
    retention_sec: int = Field(
        default=3600,
        gt=0,
        description="Сколько хранить события и тела областей, в которых тихо.",
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


class MessagingConfig(BaseModel):
    """Общие поля выбранного транспорта; конфиг реализации разбирает её фабрика."""

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)


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


AuthConfigT = TypeVar("AuthConfigT", bound=AuthConfig)


class ProcessConfig(ConfigModel):
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
    paths: PathsConfig
    tls: TlsConfig = Field(default_factory=TlsConfig)
    server: ServerConfig
    AUTH_BACKENDS: ClassVar[AuthBackends] = AuthBackends(())
    LAUNCHER_BACKENDS: ClassVar[Components[BaseModel, DictConfig, SectionLaunchers]] = (
        Components(BaseModel, "provider", ())
    )

    auth: list[SerializeAsAny[AuthConfig]]
    logger: dict[str, Any] = Field(default_factory=ProcessLogging.default)
    session: TokenConfig

    @classmethod
    def loader(cls, paths: Sequence[Path]) -> ConfigLoader:
        """Defaults и связи секций приложения до раскрытия ссылок."""
        return ConfigLoader(paths, schema=ConfigSchema(cls, values=("auth",)))

    @classmethod
    def load(cls, paths: Sequence[Path]) -> Self:
        """Читает файлы конфигурации и ставит процессу kerberos и CA."""
        raw = RawConfig.load(paths, schema=ConfigSchema(cls, values=("auth",)))
        return cls.of(raw, paths[0].resolve().parent)

    @classmethod
    def of(cls, raw: DictConfig, conf_dir: Path) -> Self:
        """Секция [app] уже загруженного дерева; conf_dir — каталог главного
        файла конфига, где лежит conf/krb. Ставит процессу kerberos и CA."""
        config = bind(raw, path=cls.SECTION, model=cls)
        # кэши билетов раскладывает приложение: строкам соединений пути не задают
        config.paths.layout(conf_dir, config.tls).apply()

        return config

    @field_validator("auth", mode="before")
    @classmethod
    def _auth_backends(cls, value: object) -> list[AuthConfig]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("[auth] expects a sequence of backend configurations")

        configs = [cls.AUTH_BACKENDS.parse(raw) for raw in value]
        cls.AUTH_BACKENDS.validate(configs)
        return configs

    def auth_entries(self, model: type[AuthConfigT]) -> tuple[AuthConfigT, ...]:
        entries = (entry for entry in self.auth if isinstance(entry, model))
        return tuple(entries)


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
    profiles: dict[str, ProfileConfig]
    session: SessionConfig
    cluster: ClusterConfig
    messaging: SerializeAsAny[MessagingConfig]
