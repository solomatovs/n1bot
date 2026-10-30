"""Стенд приложения: конфиг с правками стенда и запуск одного приложения отдельным
процессом тем же входом, что и в проде; провайдер модели, база, хранилище и журнал
уводятся на тестовые.

Конфиг стенда — config.toml дерева отладки приложения и поверх него один файл-
наложение в рабочем каталоге стенда, где лежат только изменённые ключи; процесс
получает оба файла повторным --config.

Ошибки:
StandError — стенд не поднялся, не ответил в отведённое время, конфиг отладки
    не загружается или не называет каталоги сборки runtime.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar, Generic, Protocol

import httpx
from omegaconf import DictConfig, OmegaConf
from pydantic import ValidationError
from typing_extensions import TypeVar

from boba.config import ConfigError, ConfigLoader
from boba.core.mcp_names import McpToolNames
from boba.runtime.config import ProgramDir, ProgramRoot, ProgramRoots
from boba.runtime.launchers import PluginSandbox
from boba.stand.ui.toml_text import TomlText

__all__ = [
    "StandAgentAuth",
    "StandApp",
    "StandConfig",
    "StandError",
    "StandFiles",
    "StandOverlay",
    "StandPaths",
    "StandPorts",
    "StandProcess",
    "StandRuntime",
    "StandServerName",
    "StandToolNames",
]

REPO_ROOT = Path(__file__).resolve().parents[7]


class StandError(Exception):
    """Стенд не поднялся."""


class StandPaths(StrEnum):
    """Пути репозитория, которые стенд подставляет вместо рантайма релиза:
    деревья отладки debug/<приложение> с config.toml экземпляра и наложением
    stand.toml в conf/. Каталоги сборки runtime называет конфиг (StandRuntime)."""

    AID_BASE = "debug/aid"
    MCP_BASE = "debug/mcp"
    PACKAGES = "packages"
    CONF = "conf"
    CONFIG = "config.toml"
    STAND = "stand.toml"

    def under(self, root: Path) -> Path:
        return root / self.value


class StandServerName(StrEnum):
    """Имена серверов MCP стенда чата, под которыми их получает агент треда."""

    CHAT = "chat"
    """Собственные инструменты чата в его процессе."""

    SERVICE = "aid_{endpoint}"
    """Endpoint сервиса boba-mcp: у каждого профиля чата свой."""

    def of(self, endpoint: str) -> str:
        return self.value.format(endpoint=endpoint)


class StandToolNames:
    """Полные имена инструментов стенда чата, какими их видит агент треда.

    Инструменты чата (connection_list, connection_search) принадлежат
    серверу чата, остальные — endpoint'у сервиса boba-mcp, который назван
    профилем чата. Тест называет инструмент именем на сервере, а в сценарий
    фейковой модели кладёт полное имя отсюда; форму имени держит
    McpToolNames.
    """

    CHAT_TOOLS: ClassVar[tuple[str, ...]] = ("connection_list", "connection_search")
    ENDPOINT: ClassVar[str] = "all"
    """Endpoint профиля по умолчанию: отдаёт все инструменты сервиса."""

    def __init__(self) -> None:
        self._names = McpToolNames()

    def full(self, tool: str, endpoint: str = ENDPOINT) -> str:
        if tool in self.CHAT_TOOLS:
            return self._names.full(StandServerName.CHAT.value, tool)

        return self._names.full(StandServerName.SERVICE.of(endpoint), tool)


class StandUrl(StrEnum):
    """Адрес локального стенда: сервер поднимает сам тест, TLS ему негде взять."""

    SCHEME = "http"
    HOST = "127.0.0.1"

    @classmethod
    def of(cls, port: int, path: str = "") -> str:
        return f"{cls.SCHEME}://{cls.HOST}:{port}{path}"


class StandAgentAuth(StrEnum):
    """Способ входа агента чата на сервис boba-mcp стенда: значение ключа
    auth секции [mcp.servers.<имя>.endpoint] чата."""

    PROXY = "proxy"
    LOCAL = "local"


class StandAuth(StrEnum):
    """Провайдеры входа стенда: форма логина, SSO или оба."""

    LOCAL = "local"
    LOCAL_SSO = "local+sso"
    SSO = "sso"

    @property
    def local(self) -> bool:
        return self in (StandAuth.LOCAL, StandAuth.LOCAL_SSO)

    @property
    def sso(self) -> bool:
        return self in (StandAuth.SSO, StandAuth.LOCAL_SSO)


class StandPorts:
    """Порты процессов стенда, не повторяющиеся на машине.

    Порт, который отдало ядро и который тут же отпустили, до старта процесса
    стенда успевает получить стенд другого воркера xdist или набора: тогда
    проба готовности слышит чужой сервер, а свой процесс не поднимается.
    Поэтому порт берётся вне эфемерного диапазона ядра (из него берут порты
    исходящие соединения) и закрепляется за процессом теста блокировкой
    файла до его выхода; порт, который уже кто-то слушает, пропускается.
    """

    FIRST: ClassVar[int] = 20000
    LAST: ClassVar[int] = 32000
    LOCKS: ClassVar[Path] = Path(tempfile.gettempdir()) / "boba-stand-ports"
    _held: ClassVar[list[int]] = []
    """Дескрипторы блокировок выданных портов: живут, пока жив процесс."""

    def free(self) -> int:
        """Ошибки:
        StandError — в диапазоне не осталось свободного порта.
        """
        self.LOCKS.mkdir(parents=True, exist_ok=True)
        span = self.LAST - self.FIRST
        start = secrets.randbelow(span)
        for offset in range(span):
            port = self.FIRST + (start + offset) % span
            if self._reserve(port):
                return port

        msg = (
            f"stand ports: no port in {self.FIRST}-{self.LAST} is both unlocked "
            f"in {self.LOCKS} and bindable"
        )
        raise StandError(msg)

    def _reserve(self, port: int) -> bool:
        lock = os.open(self.LOCKS / f"{port}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock)
            return False

        if not self._bindable(port):
            os.close(lock)
            return False

        self._held.append(lock)
        return True

    @staticmethod
    def _bindable(port: int) -> bool:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("", port))
            except OSError:
                return False

        return True


@dataclass(frozen=True)
class StandCredential:
    """Учётка стенда: читается из [auth.local] рабочего конфига."""

    login: str
    password: str


@dataclass(frozen=True)
class StandAppTraits:
    """Чем приложения стенда отличаются друг от друга: модуль запуска, корень
    рантайма, конфиг и проба готовности."""

    module: str
    base: StandPaths
    ready_path: str


class StandApp(StrEnum):
    """Приложения стенда: у каждого свой корень рантайма, конфиг, данные и порт."""

    AID = "aid"
    MCP = "mcp"

    def traits(self) -> StandAppTraits:
        known = {
            StandApp.AID: StandAppTraits(
                module="boba.aid.main",
                base=StandPaths.AID_BASE,
                ready_path="/",
            ),
            StandApp.MCP: StandAppTraits(
                module="boba.mcp_server",
                base=StandPaths.MCP_BASE,
                ready_path="/health",
            ),
        }

        return known[self]

    @property
    def module(self) -> str:
        return self.traits().module

    @property
    def base(self) -> StandPaths:
        return self.traits().base

    def config_path(self) -> Path:
        """config.toml экземпляра отладки приложения: главный файл процессов
        стенда."""
        conf = self.traits().base.under(REPO_ROOT) / StandPaths.CONF.value
        return conf / StandPaths.CONFIG.value

    def paths(self) -> tuple[Path, ...]:
        """Конфиг отладки глазами тестов: config.toml и наложение stand.toml
        рядом с ним, если оно есть (адреса живых сервисов, секция [stand])."""
        config = self.config_path()
        stand = config.with_name(StandPaths.STAND.value)
        if not stand.is_file():
            return (config,)

        return (config, stand)

    def tools_app(self) -> StandApp:
        """Приложение, которое исполняет инструменты этого: у чата — сервис
        boba-mcp, у остальных — оно само."""
        if self is StandApp.AID:
            return StandApp.MCP

        return self

    def raw(self) -> DictConfig:
        """Загруженный config.toml отладки без наложения стенда: из него стенд
        берёт таблицы, которые переписывает в наложении целиком."""
        config = self.config_path()
        try:
            return ConfigLoader([config]).load()
        except ConfigError as exc:
            msg = f"stand: loading the debug config of {self.value}: {exc}"
            raise StandError(msg) from exc

    def table(self, path: str) -> dict[str, Any]:
        """Таблица конфига отладки как есть, со ссылками; нет — пустая."""
        node = OmegaConf.select(self.raw(), path)
        if not isinstance(node, DictConfig):
            return {}

        table: dict[str, Any] = {}
        container = OmegaConf.to_container(node, resolve=False)
        if not isinstance(container, dict):
            return table

        for key, value in container.items():
            table[str(key)] = value

        return table

    def plugins(self) -> tuple[str, ...]:
        """Плагины конфига отладки: ключи секции [tool]."""
        return tuple(sorted(self.table("tool")))

    @property
    def ready_path(self) -> str:
        """Путь готовности под префиксом приложения."""
        return self.traits().ready_path


class RuntimeLayout(StrEnum):
    """Что стенды берут из конфигов плагинов сервиса: базовый корень песочницы
    — корень shell-плагина, а конфиги — те, что уходят в образ mcp."""

    SHELL_PLUGIN = "bash"
    PLUGIN_CONFS = "packages/apps/boba-mcp/conf/plugins"
    CONF_SUFFIX = ".toml"
    SANDBOX = "sandbox"


class StandRuntime:
    """Каталог программы на стенде: корни песочницы, программы плагинов,
    libexec и модели.

    Его называет BOBA_RUNTIME окружения тестов — тот же каталог runtime/, по
    которому сервис из исходников собирает профили песочницы (ProgramRoots),
    поэтому от положения дерева исходников он не зависит. Пути корня и
    каталогов данных плагина стенд берёт из его конфига в пакете boba-mcp
    так же, как сервер, и разрешает от того же каталога программы. Создают
    его стенды песочницы и зигот (boba.stand.zygote); тесты берут готовый
    объект оттуда.
    """

    def __init__(self) -> None:
        try:
            self._program = ProgramRoots().locate()
        except ConfigError as exc:
            msg = f"stand runtime: the program directory of the tests: {exc}"
            raise StandError(msg) from exc

        self._confs = REPO_ROOT / RuntimeLayout.PLUGIN_CONFS.value

    @property
    def program(self) -> ProgramRoot:
        return self._program

    @property
    def sandbox(self) -> Path:
        """Артефакты песочницы: шаблон workspace.ext4."""
        return self._program.path(ProgramDir.SANDBOX)

    @property
    def libexec(self) -> Path:
        """Вспомогательные программы: bwrap, fuse2fs и mke2fs."""
        return self._program.path(ProgramDir.LIBEXEC)

    def plugin_sandbox(self, plugin_id: str) -> PluginSandbox:
        """Секция [sandbox] конфига плагина plugin_id."""
        path = self._confs / f"{plugin_id}{RuntimeLayout.CONF_SUFFIX.value}"
        try:
            with path.open("rb") as handle:
                table = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            msg = f"stand runtime: reading the plugin config {path}: {exc}"
            raise StandError(msg) from exc

        try:
            return PluginSandbox.model_validate(table[RuntimeLayout.SANDBOX.value])
        except (KeyError, ValidationError) as exc:
            msg = (
                f"stand runtime: {path} expects a [sandbox] table with program "
                f"and rootfs: {exc}"
            )
            raise StandError(msg) from exc

    def plugin_rootfs(self, plugin_id: str) -> Path:
        """Образ корня плагина по его конфигу: make build APP=runtime."""
        return self._program.located(self.plugin_sandbox(plugin_id).rootfs)

    def plugin_images(self, plugin_id: str) -> tuple[str, ...]:
        """Каталоги данных плагина `каталог:точка` по его конфигу."""
        images: list[str] = []
        for image in self.plugin_sandbox(plugin_id).images:
            images.append(f"{self._program.located(image.path)}:{image.target}")

        return tuple(images)

    @property
    def workspace_template(self) -> Path:
        """Шаблон workspace сборки: make build APP=runtime."""
        return self._program.workspace_template()

    @property
    def shell_rootfs(self) -> Path:
        """Базовый корень стендов: корень shell-плагина, bash и python."""
        return self.plugin_rootfs(RuntimeLayout.SHELL_PLUGIN)


class StandService(Protocol):
    """Приложение стенда глазами процесса: конфиг, окружение, адрес и
    приложение-спутник, без которого оно не работает."""

    app: StandApp
    app_port: int
    url_prefix: str

    @property
    @abstractmethod
    def files(self) -> StandFiles:
        """Файлы конфигурации стенда: конфиг отладки и наложение уходят
        приложению повторным аргументом --config."""

    @abstractmethod
    def write(self) -> None:
        """Кладёт файлы конфигурации приложения в рабочий каталог стенда."""

    @abstractmethod
    def env(self) -> dict[str, str]:
        """Окружение процесса приложения."""

    @abstractmethod
    def companion(self) -> StandService | None:
        """Приложение, которое поднимается раньше этого; None — его нет."""


class StandOverlay:
    """Изменённые стендом ключи конфига отладки: таблица наложения.

    set кладёт значение ключа поверх конфига обычным слиянием omegaconf
    (таблица сливается, список и скаляр заменяют прежние). replace заменяет
    таблицу целиком: слияние не умеет удалять ключи, поэтому новая таблица
    ложится в секцию REPLACED, а ключ становится ссылкой на неё. Создают конфиги
    стенда на запись, пишет StandFiles.
    """

    REPLACED: ClassVar[str] = "stand_replaced"
    SEPARATOR: ClassVar[str] = "__"

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._replaced: dict[str, Any] = {}

    def set(self, path: Sequence[str], value: object) -> None:
        *parents, leaf = path
        node = self._values
        for key in parents:
            node = node.setdefault(key, {})

        node[leaf] = value

    def replace(self, path: Sequence[str], value: Mapping[str, Any]) -> None:
        name = self.SEPARATOR.join(path)
        self._replaced[name] = dict(value)
        self.set(path, f"${{{self.REPLACED}.{name}}}")

    def replaced(self, path: Sequence[str]) -> dict[str, Any]:
        """Таблица, уже положенная replace: стенд дописывает её по шагам."""
        name = self.SEPARATOR.join(path)
        return self._replaced[name]

    def document(self) -> dict[str, Any]:
        document = dict(self._values)
        if self._replaced:
            document[self.REPLACED] = dict(self._replaced)

        return document


@dataclass(frozen=True)
class StandFiles:
    """Файл-наложение приложения стенда в его рабочем каталоге.

    Процесс стенда получает два файла: config.toml отладки приложения и
    наложение с изменёнными стендом ключами (StandOverlay). Плагины не
    копируются: выключенный стендом плагин — ключ tool.<id>.enable в
    наложении.
    """

    workdir: Path

    OVERLAY: ClassVar[str] = "overlay.toml"
    SERVE: ClassVar[str] = "serve"

    @property
    def overlay(self) -> Path:
        return self.workdir / self.OVERLAY

    def paths(self, app: StandApp) -> tuple[Path, Path]:
        """Файлы процесса по порядку: конфиг отладки, затем наложение."""
        return (app.config_path(), self.overlay)

    def arguments(self, app: StandApp) -> list[str]:
        """Подкоманда serve процесса приложения и её аргументы --config."""
        arguments: list[str] = [self.SERVE]
        for path in self.paths(app):
            arguments.extend(("--config", str(path)))

        return arguments

    def write(self, overlay: StandOverlay) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.overlay.write_text(TomlText.dumps(overlay.document()), encoding="utf-8")

    def write_debug(self, app: StandApp, database: str) -> tuple[Path, Path]:
        """Наложение на конфиг отладки приложения app с базой database:
        процесс на конфиге отладки не пишет в её базу. Отдаёт файлы процесса."""
        overlay = StandOverlay()
        overlay.set(("postgres", "dbname"), database)
        if app.table("postgres_history"):
            overlay.set(("postgres_history", "dbname"), database)

        self.write(overlay)

        return self.paths(app)


@dataclass
class ServiceStand(StandService):
    """Сервис boba-mcp стенда: исполняет инструменты и держит файлы чата.

    Создаётся конфигом стенда чата (StandConfig.companion): чат ходит к нему
    клиентом MCP со входом proxy. Конфиг — рабочий конфиг сервиса с правками
    стенда: своя база плагинов, каталог файлов вместо образов workspace,
    endpoint'ы
    под профили стенда.
    """

    workdir: Path
    app_port: int
    db_name: str
    """База стенда, в которую ходят плагины сервиса (kb, ingest, describer)."""
    sandbox: bool
    app: StandApp = StandApp.MCP
    url_prefix: str = ""
    generation: str = ""
    """Поколение сессий: пустое — случайное на каждый старт процесса, и
    рестарт снимает все входы; заданное — входы рестарт переживают."""
    session_ttl_sec: int = 300
    """Срок токена доступа: коротким стенд проверяет продление входа."""
    plugins: tuple[str, ...] = ()
    """Плагины из StandPlugins.SANDBOXED, которые сервис включает, — те, что
    зовут тесты стенда: тест с соединениями пользователя включает pg, стенд
    всех инструментов — весь список. Каждая включённая секция песочницы
    держит свою зиготу, у kb и ingest — с моделью эмбеддингов."""
    agent_auth: StandAgentAuth = StandAgentAuth.PROXY
    """Каким входом чат и его агент ходят на сервис: proxy — от имени
    пользователя вкладки, local — учёткой агента AGENT_LOGIN."""

    PROXY_SECRET: ClassVar[str] = "stand-proxy-secret"  # noqa: S105 — ключ стенда
    """Ключ утверждений входа proxy: его знают сервис и чат стенда."""

    SESSION_SECRET: ClassVar[str] = "stand-service-session-secret"  # noqa: S105 — ключ стенда
    """auth_secret сервиса: свой, не равный auth_secret чата из конфига
    отладки, — так весь путь чат → сервис стенда идёт с разными секретами."""

    LOCAL_LOGIN: ClassVar[str] = "carol"
    LOCAL_PASSWORD: ClassVar[str] = "carol-pw"  # noqa: S105 — учётка стенда
    """Человек стенда для входа формой (local): роль ADM."""

    AGENT_LOGIN: ClassVar[str] = "agent"
    AGENT_PASSWORD: ClassVar[str] = "agent-pw"  # noqa: S105 — учётка стенда
    """Учётка агента для входа local без человека: роль ADM."""

    CLIENT_ID: ClassVar[str] = "stand-chat"
    CLIENT_SECRET: ClassVar[str] = "stand-client-secret"  # noqa: S105 — ключ стенда
    """Клиент OAuth чата стенда у сервера авторизации сервиса."""

    ENDPOINTS: ClassVar[Mapping[str, Sequence[str]]] = {
        "all": ("*",),
        "search": ("diagram_save", "canvas_open"),
    }
    """Endpoint'ы стенда: all — все инструменты, search — узкий набор."""

    PROFILE_ENDPOINTS: ClassVar[Mapping[str, str]] = {
        "general": "all",
        "search": "search",
    }
    """Endpoint сервиса у каждого профиля чата стенда."""

    SEALING: ClassVar[Mapping[str, Any]] = {
        "roles": ["*"],
        "client": "sealing",
        "offer": ["sealed"],
    }
    """Правило предложения соединений endpoint'ов стенда: клиенту, который
    запечатывает, — соединения пользователя, как в рабочем конфиге."""

    HEADERS: ClassVar[Mapping[str, str]] = {
        "user": "X-Remote-User",
        "timestamp": "X-Boba-Timestamp",
        "signature": "X-Boba-Signature",
    }
    ROLES_HEADER: ClassVar[str] = "X-Remote-Roles"

    @property
    def files(self) -> StandFiles:
        return StandFiles(self.workdir)

    @property
    def data_dir(self) -> Path:
        return self.workdir / "data"

    def companion(self) -> StandService | None:
        return None

    def server_name(self, endpoint: str) -> str:
        """Имя сервера endpoint'а в секции [mcp.servers] чата."""
        return StandServerName.SERVICE.of(endpoint)

    def server(self, endpoint: str) -> dict[str, Any]:
        """Секция [mcp.servers.<имя>] чата для endpoint'а сервиса."""
        service = {
            "scheme": StandUrl.SCHEME.value,
            "host": StandUrl.HOST.value,
            "port": self.app_port,
            "path": "",
        }

        return {
            "limits": {"connect_timeout_sec": 30.0, "call_timeout_sec": 120.0},
            "endpoint": {
                "transport": "streamable-http",
                "service": service,
                "path": f"/{endpoint}",
                "auth": self._auth(service),
            },
        }

    def _auth(self, service: Mapping[str, Any]) -> dict[str, Any]:
        """Секция auth endpoint'а по способу входа агента стенда."""
        entries: dict[StandAgentAuth, dict[str, Any]] = {
            StandAgentAuth.PROXY: {
                "auth": StandAgentAuth.PROXY.value,
                "issuer": dict(service),
                "client_id": self.CLIENT_ID,
                "client_secret": self.CLIENT_SECRET,
                "secret": self.PROXY_SECRET,
            },
            StandAgentAuth.LOCAL: {
                "auth": StandAgentAuth.LOCAL.value,
                "login": self.AGENT_LOGIN,
                "password": self.AGENT_PASSWORD,
            },
        }

        return entries[self.agent_auth]

    def write(self) -> None:
        overlay = StandOverlay()
        overlay.set(("postgres", "dbname"), self.db_name)
        overlay.set(("postgres", "pool", "min_size"), 1)
        overlay.set(("postgres", "pool", "max_size"), 6)

        if not self.sandbox:
            files_dir = self.workdir / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            overlay.set(("storage", "kind"), "local")
            overlay.set(("storage", "files_dir"), str(files_dir))

        endpoints: dict[str, Any] = {}
        for name, tools in self.ENDPOINTS.items():
            endpoints[name] = {
                "roles": ["*"],
                "tools": list(tools),
                "connections": [dict(self.SEALING)],
                "prompts": {},
            }

        overlay.replace(("mcp", "endpoints"), endpoints)
        overlay.set(("mcp", "public_url"), f"http://127.0.0.1:{self.app_port}")
        overlay.set(("mcp", "url_prefix"), "")
        overlay.replace(
            ("mcp", "clients"), {self.CLIENT_ID: {"secret": self.CLIENT_SECRET}}
        )
        overlay.replace(
            ("auth", "proxy"),
            {
                "type": "proxy",
                "path": "/auth/proxy",
                "secret": self.PROXY_SECRET,
                "max_skew_sec": 60,
                "allowed_clients": ["0.0.0.0/0"],
                "require_roles": True,
                "headers": dict(self.HEADERS),
                "roles": {"header": {"name": self.ROLES_HEADER}},
            },
        )
        overlay.replace(
            ("auth", "local"),
            {
                "type": "local",
                "users": {
                    self.LOCAL_LOGIN: self.LOCAL_PASSWORD,
                    self.AGENT_LOGIN: self.AGENT_PASSWORD,
                },
                "require_roles": True,
                "roles": {
                    "local": {
                        "mapping": {
                            self.LOCAL_LOGIN: ["ADM"],
                            self.AGENT_LOGIN: ["ADM"],
                        }
                    }
                },
            },
        )
        overlay.set(("app", "auth"), ["${auth.local}", "${auth.proxy}"])
        overlay.set(("keys", "session"), self.SESSION_SECRET)
        overlay.set(("session", "generation"), self.generation)
        overlay.set(("session", "session_ttl_sec"), self.session_ttl_sec)
        StandPlugins(self.app, self.plugins).disable(overlay)
        StandEnv(self.app, self.data_dir).put(overlay, self.app_port, self.sandbox)

        self.files.write(overlay)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        # BOBA_CGROUP_BASE наследуется от прогона: поддерево делегирует user-юнит
        env["PGGSSENCMODE"] = "disable"
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("BOBA_CONFIG_PATH", None)
        env.pop("KRB5_CLIENT_KTNAME", None)
        env.pop("KRB5CCNAME", None)

        return env


class StandEnv:
    """Свой каталог данных ([instance].data_dir) и секция [env] наложения
    приложения стенда: порт, имя инстанса и способ запуска инструментов.
    Создают конфиги стенда на запись; каталоги данных, которые приложение
    ждёт готовыми, заводит put."""

    DATA_DIRS: ClassVar[tuple[str, ...]] = (
        "workspace",
        "tool-logs",
        "dump",
        "krb",
        "agents",
        "files",
    )

    def __init__(self, app: StandApp, data_dir: Path) -> None:
        self._app = app
        self._data_dir = data_dir

    def put(self, overlay: StandOverlay, port: int, sandbox: bool) -> None:
        for name in self.DATA_DIRS:
            (self._data_dir / name).mkdir(parents=True, exist_ok=True)

        # способ запуска tools фиксируется стендом, а не конфигом отладки
        launcher = "process"
        if sandbox:
            launcher = "sandbox"

        overlay.set(("instance", "data_dir"), str(self._data_dir))
        overlay.set(("env", "port"), port)
        overlay.set(("env", "instance_id"), f"stand{port}")
        overlay.set(("env", "tool_launcher"), launcher)


class StandPlugins:
    """Плагины приложения стенда: те из SANDBOXED, которые стенд не включил,
    выключаются ключом tool.<id>.enable наложения.

    Создаётся конфигом приложения на запись. У чата плагинов нет:
    инструменты исполняет сервис boba-mcp. Из SANDBOXED у сервиса включены
    только названные стендом: остальным нужны песочница или внешние
    сервисы, а в песочнице каждая секция — своя зигота.
    """

    SANDBOXED: ClassVar[tuple[str, ...]] = (
        "bash",
        "doc",
        "chart",
        "web",
        "confluence",
        "ingest",
        "kb",
        "pg",
        "ch",
        "ora",
        "describer",
    )
    """Инструменты, которым нужна песочница или внешние сервисы."""

    def __init__(self, app: StandApp, enabled: Sequence[str]) -> None:
        self._app = app
        self._enabled_plugins = frozenset(enabled)

    def disable(self, overlay: StandOverlay) -> None:
        for plugin in self._app.plugins():
            if self._enabled(plugin):
                continue

            overlay.set(("tool", plugin, "enable"), False)

    def _enabled(self, plugin: str) -> bool:
        if plugin in self._enabled_plugins:
            return True

        return plugin not in self.SANDBOXED


@dataclass
class StandConfig(StandService):
    """Конфиг стенда: пишет свой config.toml и env для дочернего процесса."""

    workdir: Path
    app: StandApp
    app_port: int
    """Порт приложения стенда: единственный адрес браузера и тестов."""

    llm_port: int
    db_name: str
    url_prefix: str = "/boba-test"
    single_profile: bool = False
    """True — в конфиге остаётся один профиль: селектора в UI быть не должно."""

    sandbox: bool = False
    """True — инструменты песочницы остаются включёнными: боевой путь целиком."""

    auth: StandAuth = StandAuth.LOCAL
    """Набор провайдеров входа стенда."""

    sso_roles: dict[str, list[str]] = field(default_factory=dict)
    """Роли SSO-входа по принципалу: без них вход отклоняется как безролевой."""

    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """MCP-серверы стенда по именам, как секции [mcp.servers.<имя>]; внешние
    серверы рабочего конфига стенд не наследует."""

    service_port: int = field(default_factory=StandPorts().free)
    """Порт сервиса boba-mcp стенда чата."""

    agent_history: dict[str, Any] = field(
        default_factory=lambda: {
            "kind": "postgres",
            "schema": "boba",
            "postgres": "${postgres_history}",
        }
    )
    """Секция [agent.history] чата: по умолчанию Postgres стенда подключением
    истории [postgres_history] рабочего конфига, схема boba в базе стенда."""

    agent_process: dict[str, Any] = field(
        default_factory=lambda: {
            "command": shlex.join([sys.executable, "-m", StandConfig.AGENT_MODULE]),
            "args": ["--stdio", "--setting-sources", ""],
        }
    )
    """Команда строкой оболочки и аргументы секции [agent.process] чата: по
    умолчанию агент boba.cli.app интерпретатором прогона; тест чужого агента
    ставит свою."""

    agent_service: dict[str, Any] = field(default_factory=dict)
    """Секция [agent.process] службы агента (адрес ACP и вход proxy): пусто —
    агент процессом по agent_process."""

    agent_timeouts_sec: tuple[float, float] = (60.0, 60.0)
    """Сроки рукопожатия и ответа на метод сессии агента чата."""

    generation: str = ""
    """Поколение сессий: пустое — случайное на каждый старт процесса; общее у
    двух инстансов стенда даёт им принимать одну и ту же cookie входа."""

    service_plugins: tuple[str, ...] = ()
    """Плагины из StandPlugins.SANDBOXED, которые включает сервис boba-mcp
    стенда (ServiceStand.plugins)."""
    agent_auth: StandAgentAuth = StandAgentAuth.PROXY
    """Способ входа чата и его агента на сервис boba-mcp стенда."""
    service_token_ttl_sec: int = 300
    """Срок токена доступа сервиса стенда: коротким тест проверяет, что
    агент обновляет токен сам."""
    messaging: str = ""
    """Провайдер шины [messaging]: пусто — как в конфиге отладки (память
    процесса); postgres — шина в базе стенда, общая у двух инстансов."""
    runtime: str = ""
    """Каталог runtime/ процесса чата (BOBA_RUNTIME): пусто — тот же, что у
    прогона; runtime/ своего дерева исходников со своими ассетами чата даёт
    стенду другой конфиг chainlit (язык)."""

    CHAT_TOOLS: ClassVar[tuple[str, ...]] = StandToolNames.CHAT_TOOLS
    """Инструменты, которые чат исполняет сам; остальные — у сервиса boba-mcp."""
    AGENT_MODULE: ClassVar[str] = "boba.cli.app"
    """Модуль агента ACP: стенд запускает его интерпретатором прогона."""

    def companion(self) -> StandService | None:
        """Сервис boba-mcp чата стенда: инструменты и файлы живут у него."""
        return self.service()

    def service(self) -> ServiceStand | None:
        if self.app is not StandApp.AID:
            return None

        return ServiceStand(
            workdir=self.workdir / "mcp",
            app_port=self.service_port,
            db_name=self.db_name,
            sandbox=self.sandbox,
            plugins=self.service_plugins,
            agent_auth=self.agent_auth,
            session_ttl_sec=self.service_token_ttl_sec,
        )

    @property
    def files(self) -> StandFiles:
        return StandFiles(self.workdir)

    @property
    def data_dir(self) -> Path:
        """Данные приложения стенда: образы workspace, журналы, kerberos-кэши."""
        return self.workdir / "data"

    @property
    def base_url(self) -> str:
        return StandUrl.of(self.app_port, self.url_prefix)

    STAND_USERS: ClassVar[dict[str, str]] = {
        "admin": "stand-admin-pass",
        "dev": "stand-dev-pass",
        "guest": "stand-guest-pass",
        "norole": "stand-norole-pass",
    }
    """Учётки стенда: фиксированы кодом, рабочий конфиг их не задаёт. У norole
    ролей нет: её вход отказан, как у учётной записи без ролей."""

    STAND_ROLES: ClassVar[dict[str, list[str]]] = {
        "admin": ["ADM"],
        "dev": ["DEV"],
        "guest": ["GST"],
    }
    """Роли учёток стенда: согласованы с [roles] стенда, а не рабочего конфига.
    GST — роль без инструментов и без прав на каталог: ей вид открывают шарингом."""

    def credential(self, login: str = "") -> StandCredential:
        """Логин и пароль стенда; без аргумента — первый логин по алфавиту."""
        logins = sorted(self.STAND_USERS)
        if not login:
            login = logins[0]

        if login not in self.STAND_USERS:
            msg = f"stand credential: login {login!r} is not among stand users {logins}"
            raise StandError(msg)

        return StandCredential(login=login, password=self.STAND_USERS[login])

    def local_users(self) -> dict[str, list[str]]:
        """Логины стенда и их роли."""
        found: dict[str, list[str]] = {}
        for login in self.STAND_USERS:
            found[login] = list(self.STAND_ROLES.get(login, []))

        return found

    def write(self) -> None:
        """Кладёт наложение с правками стенда в рабочий каталог."""
        overlay = StandOverlay()

        self._use_fake_llm(overlay)
        self._use_test_profiles(overlay)
        self._use_test_database(overlay)
        self._use_local_storage(overlay)
        self._without_mcp_servers(overlay)
        self._use_local_auth(overlay)
        self._use_agent(overlay)
        self._shrink_pools(overlay)
        self._use_env(overlay)
        overlay.set(("session", "generation"), self.generation)
        StandPlugins(self.app, ()).disable(overlay)

        self.files.write(overlay)

    def _use_agent(self, overlay: StandOverlay) -> None:
        """Агент ACP стенда чата: процесс и история по полям стенда,
        приватные каталоги — agents/ данных стенда; инструменты «всегда
        загружать» и пределы хода — как в конфиге приложения."""
        agent = self.app.table("agent")
        if not agent:
            return

        overlay.set(("postgres_history", "dbname"), self.db_name)
        start_sec, request_sec = self.agent_timeouts_sec
        process: dict[str, Any] = {
            "command": self.agent_process["command"],
            "args": self.agent_process["args"],
            "start_timeout_sec": start_sec,
            "request_timeout_sec": request_sec,
            "stop_timeout_sec": 10,
        }
        if self.agent_service:
            process = {
                **self.agent_service,
                "start_timeout_sec": start_sec,
                "request_timeout_sec": request_sec,
                "silence_timeout_sec": 60,
            }

        overlay.replace(
            ("agent",),
            {
                "public_url": self.base_url,
                "seal_ttl_sec": 600,
                "question_timeout_sec": 600,
                "process": process,
                "history": dict(self.agent_history),
                "tools": agent["tools"],
                "limits": agent["limits"],
            },
        )

    @staticmethod
    def _shrink_pools(overlay: StandOverlay) -> None:
        """Стенды живут по нескольку сразу: малые пулы не исчерпывают слоты Postgres."""
        overlay.set(("postgres", "pool", "min_size"), 1)
        overlay.set(("postgres", "pool", "max_size"), 6)

    def _use_env(self, overlay: StandOverlay) -> None:
        """Каталог данных и секция [env] стенда: порт, имя инстанса, префикс и
        шина, если стенд её задал."""
        StandEnv(self.app, self.data_dir).put(overlay, self.app_port, self.sandbox)
        overlay.set(("env", "url_prefix"), self.url_prefix)
        if self.messaging:
            overlay.set(("env", "messaging_provider"), self.messaging)

    def env(self) -> dict[str, str]:
        """Окружение процесса приложения: конфиг целиком в файлах --config,
        окружение несёт только то, что читают библиотеки."""
        env = dict(os.environ)
        # BOBA_CGROUP_BASE наследуется от прогона: поддерево делегирует user-юнит
        env["PGGSSENCMODE"] = "disable"
        # лог стенда читает упавший тест: буфер до kill не доживёт
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("KRB5_CLIENT_KTNAME", None)
        env.pop("KRB5CCNAME", None)
        if self.runtime:
            env[ProgramRoots.RUNTIME_ENV] = self.runtime

        return env

    def _use_fake_llm(self, overlay: StandOverlay) -> None:
        """Провайдер стенда: fake llm процесса тестов рядом с секциями [llm]
        конфига; профили стенда ссылаются только на него."""
        overlay.set(("agent_http", "dump", "enable"), False)
        overlay.set(
            ("model_limits", "fake"),
            {
                "context_window": 200000,
                "max_output_tokens": 4096,
                "reasoning_return": "turn",
            },
        )
        overlay.set(
            ("llm", "fake"),
            {
                "kind": "openai",
                "transport": "${agent_http}",
                "connection": {
                    "scheme": "http",
                    "host": StandUrl.HOST.value,
                    "port": self.llm_port,
                    "path": "/v1",
                    "ssl_verify": False,
                    "auth": {"method": "bearer", "token": "none"},
                },
            },
        )

    def _use_test_profiles(self, overlay: StandOverlay) -> None:
        """Профили и роли стенда: фиксированные, тесты знают их наизусть.

        general — все инструменты, search — узкий набор; DEV-роль покрывает
        не все инструменты, чтобы было видно пересечение роли и профиля. Сценарий
        fake llm выбирается по тексту сообщения, поэтому имя модели свободно.
        """
        # у чата роль режет только его собственные инструменты: DEV без поиска
        # соединений; набор инструментов сервиса решает endpoint, не роль
        own = ["connection_list"]
        if self.app is not StandApp.AID:
            own = ["diagram_save", "send_file", *self.CHAT_TOOLS]

        overlay.replace(
            ("roles",),
            {
                "ADM": {"tools": ["*"]},
                "DEV": {"tools": own},
                "GST": {"tools": []},
            },
        )

        profiles: dict[str, Any] = {
            "general": {
                "display_name": "Общий",
                "description": "Профиль стенда со всеми инструментами",
                "default": True,
                "roles": ["*"],
                "tools": ["*"],
                "provider": "${llm.fake}",
                "model": "fake-model-general",
                "limits": "${model_limits.fake}",
                "system_prompt": "You are the general stand assistant",
                "sampling": {
                    "temperature": 0.1,
                    "max_completion_tokens": 1111,
                    "top_p": 0.9,
                },
            },
            "search": {
                "display_name": "Поиск",
                "description": "Профиль стенда с узким набором инструментов",
                "default": False,
                "roles": ["*"],
                "tools": self._search_tools(),
                "provider": "${llm.fake}",
                "model": "fake-model-search",
                "limits": "${model_limits.fake}",
                "system_prompt": "You are the search stand assistant",
                "sampling": {
                    "temperature": 0.7,
                    "max_completion_tokens": 2222,
                },
            },
        }

        if self.single_profile:
            profiles = {"general": profiles["general"]}

        overlay.replace(("profiles",), profiles)

    def _search_tools(self) -> list[str]:
        """Свои инструменты профиля search: у чата узкий набор отдаёт endpoint
        сервиса, у сервиса — его собственные инструменты."""
        if self.app is StandApp.AID:
            return []

        return ["diagram_save", "canvas_open"]

    def _use_test_database(self, overlay: StandOverlay) -> None:
        """Сервер и учётка — из конфига приложения; стенду — отдельная база.

        Таблица соединений живёт там же: стенд сеет её после старта.
        """
        overlay.set(("postgres", "dbname"), self.db_name)

    def _use_local_storage(self, overlay: StandOverlay) -> None:
        """Файлы стенда: без песочницы — каталог на диске; с песочницей — образы
        workspace, как в бою: тела пишут в образ, и хост обязан читать его же."""
        files_dir = self.workdir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        if self.app.table("storage") and not self.sandbox:
            overlay.set(("storage", "kind"), "local")
            overlay.set(("storage", "files_dir"), str(files_dir))

    def _without_mcp_servers(self, overlay: StandOverlay) -> None:
        """Внешние MCP-серверы рабочего конфига стенду не нужны: набор
        инструментов сессии не должен зависеть от сети. Остаются только
        серверы, заданные самим стендом: их называет каждый профиль стенда.
        Песочницы серверов с секцией apps встраивает чат стенда: адрес его
        страницы — chat_url каждой такой секции."""
        service = self.service()
        servers: dict[str, dict[str, Any]] = {}
        own: dict[str, str] = {}
        if service is not None:
            for name, endpoint in service.PROFILE_ENDPOINTS.items():
                own[name] = service.server_name(endpoint)
                servers[own[name]] = service.server(endpoint)

        for name, section in self.mcp_servers.items():
            servers[name] = self._embedded(section)

        overlay.replace(("mcp",), {"servers": servers})
        for name, profile in overlay.replaced(("profiles",)).items():
            named: list[str] = []
            if name in own:
                named.append(own[name])

            named.extend(sorted(self.mcp_servers))
            profile["mcp"] = named

    def _embedded(self, section: Mapping[str, Any]) -> dict[str, Any]:
        """Секция [mcp.servers.<имя>] с адресом чата стенда в секции apps."""
        embedded = dict(section)
        if apps := section.get("apps"):
            embedded["apps"] = {**apps, "chat_url": self.base_url}

        return embedded

    def _use_local_auth(self, overlay: StandOverlay) -> None:
        """Учётки и роли стенда: рабочий [auth.local] не трогаем и не наследуем."""
        providers: list[str] = []

        if self.auth.sso:
            # keytab, SPN и делегирование — из рабочего конфига; маппинг ролей
            # через LDAP стенду не нужен
            providers.append("${auth.kerberos}")
            kerberos = self.app.table("auth.kerberos")
            kerberos.pop("roles", None)
            if self.sso_roles:
                kerberos["roles"] = {"principal": {"principal": dict(self.sso_roles)}}

            overlay.replace(("auth", "kerberos"), kerberos)

        if self.auth.local:
            providers.append("${auth.local}")
            mapping = {login: list(roles) for login, roles in self.STAND_ROLES.items()}
            overlay.replace(
                ("auth", "local"),
                {
                    "type": "local",
                    "users": dict(self.STAND_USERS),
                    "roles": {"local": {"mapping": mapping}},
                    "require_roles": True,
                },
            )

        overlay.set(("app", "auth"), providers)


ConfigT = TypeVar("ConfigT", bound=StandService, default=StandConfig)


@dataclass
class StandProcess(Generic[ConfigT]):
    """Процесс приложения стенда: живёт на время сессии тестов."""

    config: ConfigT
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    companion: StandProcess[StandService] | None = None
    """Процесс приложения-спутника: поднимается раньше, гаснет позже."""

    COMPLAINT_MARKERS: ClassVar[tuple[str, ...]] = (
        "ERROR:",
        "Traceback",
        "Task exception was never retrieved",
        "loop mismatch",
    )
    """Маркеры строк, которых в логе живого хода быть не должно."""

    def start(self, boot_timeout_sec: float) -> None:
        """Поднимает процесс; неудачный старт гасит его."""
        try:
            self._start(boot_timeout_sec)
        except Exception:
            self.stop()
            raise

    def _start(self, boot_timeout_sec: float) -> None:
        needed = self.config.companion()
        if needed is not None:
            log_path = self.log_path.with_name(f"{needed.app.value}.log")
            self.companion = StandProcess[StandService](
                config=needed, log_path=log_path
            )
            self.companion.start(boot_timeout_sec)

        self.config.write()
        self.process = self._spawn()
        deadline = time.monotonic() + boot_timeout_sec
        path = self.config.url_prefix + self.config.app.ready_path
        self._await_ready(
            self.process, StandUrl.of(self.config.app_port, path), deadline
        )

    def _spawn(self) -> subprocess.Popen[bytes]:
        handle = self.log_path.open("wb")
        return subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                self.config.app.module,
                *self.config.files.arguments(self.config.app),
            ],
            env=self.config.env(),
            cwd=str(REPO_ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        self._stop_own()
        if self.companion is not None:
            self.companion.stop()

    def _stop_own(self) -> None:
        if self.process is None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def log_lines(self) -> int:
        """Сколько строк в логе приложения сейчас: отметка начала хода."""
        if not self.log_path.is_file():
            return 0

        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return len(text.splitlines())

    def complaints(self, since_line: int = 0) -> list[str]:
        """Строки лога приложения об ошибках начиная с отметки: ход обязан
        проходить без них, а чужие ходы в общем логе не считаются."""
        if not self.log_path.is_file():
            return []

        text = self.log_path.read_text(encoding="utf-8", errors="replace")

        found: list[str] = []
        for line in text.splitlines()[since_line:]:
            for marker in self.COMPLAINT_MARKERS:
                if marker in line:
                    found.append(line)
                    break

        return found

    def tail(self, lines: int = 40) -> str:
        """Хвост лога процесса: без него падение теста молчит о причине."""
        if not self.log_path.is_file():
            return ""

        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def _await_ready(
        self, process: subprocess.Popen[bytes], url: str, deadline: float
    ) -> None:
        app = self.config.app.value
        while time.monotonic() < deadline:
            if process.poll() is not None:
                code = process.returncode
                msg = (
                    f"{app} exited with code {code} before answering at {url}:"
                    f"\n{self.tail()}"
                )
                raise StandError(msg)

            try:
                response = httpx.get(url, timeout=2.0)
            except httpx.HTTPError:
                time.sleep(0.3)
                continue

            if response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
                return

            time.sleep(0.3)

        msg = (
            f"GET {url}: {app} gave no reply below 500 before the start deadline:"
            f"\n{self.tail()}"
        )
        raise StandError(msg)
