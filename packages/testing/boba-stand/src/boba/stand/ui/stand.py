"""Стенд приложения: конфиг с правками стенда и запуск одного приложения отдельным
процессом тем же входом, что и в проде; провайдер модели, база, хранилище и журнал
уводятся на тестовые.

Ошибки:
StandError — стенд не поднялся, не ответил в отведённое время или site.toml
    отладки сервиса не называет каталоги сборки runtime.
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
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar, Generic, Protocol

import httpx
from pydantic import ValidationError
from typing_extensions import TypeVar

from boba.config import bind
from boba.core.mcp_names import McpToolNames
from boba.runtime.config import AppLayers, ConfigFiles
from boba.runtime.launchers import EnvPaths
from boba.stand.ui.toml_text import TomlText

__all__ = [
    "StandAgentAuth",
    "StandApp",
    "StandConfig",
    "StandError",
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
    общие конфиги приложений — в их пакетах, базы и site.toml — деревья отладки
    debug/<приложение>. Каталоги сборки runtime называет конфиг (StandRuntime)."""

    AID_CONF = "packages/apps/boba-aid/conf"
    MCP_CONF = "packages/apps/boba-mcp/conf"
    AID_BASE = "debug/aid"
    MCP_BASE = "debug/mcp"
    PACKAGES = "packages"

    def under(self, root: Path) -> Path:
        return root / self.value


class StandServerName(StrEnum):
    """Имена серверов MCP стенда чата, под которыми их получает агент треда."""

    CHAT = "chat"
    """Собственные инструменты чата в его процессе."""

    SERVICE = "boba_{endpoint}"
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
    ENDPOINT: ClassVar[str] = "general"
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
    conf: StandPaths
    """Каталог общего конфига приложения и его плагинов в пакете."""
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
                conf=StandPaths.AID_CONF,
                ready_path="/",
            ),
            StandApp.MCP: StandAppTraits(
                module="boba.mcp_server",
                base=StandPaths.MCP_BASE,
                conf=StandPaths.MCP_CONF,
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

    def files(self) -> ConfigFiles:
        """Файлы конфигурации приложения на стенде: общий конфиг пакета и
        site.toml дерева отладки."""
        conf = self.traits().conf.under(REPO_ROOT)
        base = self.traits().base.under(REPO_ROOT)

        return ConfigFiles(
            config=conf / "config.toml", site=base / "conf" / "site.toml"
        )

    def tools_app(self) -> StandApp:
        """Приложение, которое исполняет инструменты этого: у чата — сервис
        boba-mcp, у остальных — оно само."""
        if self is StandApp.AID:
            return StandApp.MCP

        return self

    def document(self) -> dict[str, Any]:
        """Общий конфиг и site.toml приложения одной таблицей: стенд правит её
        и раскладывает обратно на два файла (StandFiles.write)."""
        files = self.files()
        with files.config.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        with files.site.open("rb") as handle:
            doc.update(tomllib.load(handle))

        return doc

    @property
    def ready_path(self) -> str:
        """Путь готовности под префиксом приложения."""
        return self.traits().ready_path


class RuntimeLayout(StrEnum):
    """Раскладка каталогов сборки runtime: образы плагинов песочницы и
    бинарники развёртывания."""

    PLUGINS = "plugins"
    ROOTFS = "rootfs.ext4"
    WORKSPACE = "workspace.ext4"
    SHELL_PLUGIN = "boba-tool-shell"
    BIN = "bin"


class StandRuntime:
    """Каталоги сборки runtime на стенде: образы песочницы, бинарники third и
    модели плагинов.

    Их называет секция [env] site.toml отладки сервиса (debug/mcp/conf) — те же
    ключи sandbox, third и models, по которым сервис собирает профили
    песочницы, поэтому от положения дерева исходников они не зависят. Создают
    стенды песочницы и зигот (boba.stand.zygote); тесты берут готовый объект
    оттуда.
    """

    def __init__(self) -> None:
        files = StandApp.MCP.files()
        raw = AppLayers.compose(files)
        try:
            self._paths = bind(raw, path="env", model=EnvPaths)
        except ValidationError as exc:
            msg = (
                f"stand runtime: [env] of {files.site} expects the sandbox, "
                f"third and models directories of the build, got: {exc}"
            )
            raise StandError(msg) from exc

    @property
    def sandbox(self) -> Path:
        """Артефакты песочницы: образы плагинов и workspace.ext4."""
        return Path(self._paths.sandbox)

    @property
    def third(self) -> Path:
        """Сторонние зависимости развёртывания: bin и lib."""
        return Path(self._paths.third)

    @property
    def third_bin(self) -> Path:
        """Бинарники развёртывания: bwrap и fuse2fs из make sandbox."""
        return self.third / RuntimeLayout.BIN

    @property
    def models(self) -> Path:
        """Модели плагинов: fastembed, rapidocr и прочие."""
        return Path(self._paths.models)

    def plugin_rootfs(self, package: str) -> Path:
        """Образ корня пакета: make plugin-rootfs PLUGIN=<пакет>."""
        return self.sandbox / RuntimeLayout.PLUGINS / package / RuntimeLayout.ROOTFS

    @property
    def workspace_template(self) -> Path:
        """Шаблон workspace сборки: make sandbox делает его mke2fs из tools."""
        return self.sandbox / RuntimeLayout.WORKSPACE

    @property
    def shell_rootfs(self) -> Path:
        """Базовый корень стендов: образ shell-плагина, bash и python без
        payload'ов."""
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
        """Файлы конфигурации стенда: уходят приложению аргументами --config и
        --site."""

    @abstractmethod
    def write(self) -> None:
        """Кладёт файлы конфигурации приложения в рабочий каталог стенда."""

    @abstractmethod
    def env(self) -> dict[str, str]:
        """Окружение процесса приложения."""

    @abstractmethod
    def companion(self) -> StandService | None:
        """Приложение, которое поднимается раньше этого; None — его нет."""


@dataclass(frozen=True)
class StandFiles:
    """Файлы конфигурации приложения стенда в его рабочем каталоге: общий конфиг
    с правками стенда, плагины рядом с ним и site-файл.

    Правки стенда делаются над одной таблицей (StandApp.document); на запись
    секции [env] и [site] уходят в site-файл, остальные — в общий конфиг: так
    их и ждёт загрузчик.
    """

    workdir: Path

    SITE_SECTIONS: ClassVar[tuple[str, ...]] = ("env", "site")

    @property
    def config(self) -> Path:
        return self.workdir / "config.toml"

    @property
    def site(self) -> Path:
        return self.workdir / "site.toml"

    @property
    def plugins(self) -> Path:
        return self.workdir / "plugins"

    def write(self, doc: Mapping[str, Any]) -> None:
        site: dict[str, Any] = {}
        common: dict[str, Any] = {}
        for section, body in doc.items():
            if section in self.SITE_SECTIONS:
                site[section] = body
            else:
                common[section] = body

        self.config.write_text(TomlText.dumps(common), encoding="utf-8")
        self.site.write_text(TomlText.dumps(site), encoding="utf-8")

    def write_debug(self, app: StandApp, database: str) -> ConfigFiles:
        """Конфиг, site.toml и плагины отладки приложения app в этом каталоге,
        но с базой database: процесс на конфиге отладки не пишет в её базу.

        Пути site.toml считаются от base отладки, поэтому процессу нужен
        BOBA_BASE со значением app.files().base().
        """
        doc = app.document()
        doc["postgres"]["dbname"] = database
        if "postgres_history" in doc:
            doc["postgres_history"]["dbname"] = database

        self.workdir.mkdir(parents=True, exist_ok=True)
        self.write(doc)
        StandPlugins(app, sandbox=True).copy_to(self.plugins)

        return ConfigFiles(config=self.config, site=self.site)


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
    """Плагины сверх набора стенда без песочницы: тест с соединениями
    пользователя включает pg, его тела идут процессами хоста."""
    agent_auth: StandAgentAuth = StandAgentAuth.PROXY
    """Каким входом чат и его агент ходят на сервис: proxy — от имени
    пользователя вкладки, local — учёткой агента AGENT_LOGIN."""

    PROXY_SECRET: ClassVar[str] = "stand-proxy-secret"  # noqa: S105 — ключ стенда
    """Ключ утверждений входа proxy: его знают сервис и чат стенда."""

    SESSION_SECRET: ClassVar[str] = "stand-service-session-secret"  # noqa: S105 — ключ стенда
    """auth_secret сервиса: свой, не равный auth_secret чата из site.toml
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
        "general": ("*",),
        "search": ("diagram_save", "canvas_open"),
    }
    """Endpoint'ы стенда по профилям чата: general — все инструменты,
    search — узкий набор."""

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
                "path": f"/mcp/{endpoint}",
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
        self.workdir.mkdir(parents=True, exist_ok=True)
        doc = self.app.document()

        doc["postgres"]["dbname"] = self.db_name
        pool = doc["postgres"]["pool"]
        pool["min_size"] = 1
        pool["max_size"] = 6

        journal_dir = self.workdir / "tool-logs"
        journal_dir.mkdir(parents=True, exist_ok=True)
        doc["stream_journal"]["dir"] = str(journal_dir)

        if not self.sandbox:
            files_dir = self.workdir / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            doc["storage"]["kind"] = "local"
            doc["storage"]["files_dir"] = str(files_dir)

        endpoints: dict[str, Any] = {}
        for name, tools in self.ENDPOINTS.items():
            endpoints[name] = {
                "roles": ["*"],
                "tools": list(tools),
                "connections": [dict(self.SEALING)],
            }

        doc["mcp"]["endpoints"] = endpoints
        doc["mcp"]["public_url"] = f"http://127.0.0.1:{self.app_port}"
        doc["mcp"]["clients"] = {self.CLIENT_ID: {"secret": self.CLIENT_SECRET}}
        doc["auth"]["proxy"] = {
            "type": "proxy",
            "path": "/auth/proxy",
            "secret": self.PROXY_SECRET,
            "max_skew_sec": 60,
            "allowed_clients": ["0.0.0.0/0"],
            "require_roles": True,
            "headers": dict(self.HEADERS),
            "roles": {"header": {"name": self.ROLES_HEADER}},
        }
        doc["auth"]["local"] = {
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
        }
        doc["app"]["auth"] = ["${auth.local}", "${auth.proxy}"]
        doc["site"]["auth_secret"] = self.SESSION_SECRET
        doc["session"]["generation"] = self.generation
        doc["session"]["session_ttl_sec"] = self.session_ttl_sec

        self.files.write(doc)
        StandPlugins(self.app, self.sandbox, self.plugins).copy_to(self.files.plugins)

    def env(self) -> dict[str, str]:
        data_dir = self.data_dir
        for name in ("workspace", "tool-logs", "dump", "krb"):
            (data_dir / name).mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["BOBA_BASE"] = str(self.app.base.under(REPO_ROOT))
        env["BOBA_DATA"] = str(data_dir)
        # BOBA_CGROUP_BASE наследуется от прогона: поддерево делегирует user-юнит
        env["BOBA_PORT"] = str(self.app_port)
        env["BOBA_INSTANCE_ID"] = f"stand{self.app_port}"
        env["PGGSSENCMODE"] = "disable"
        if self.sandbox:
            env["BOBA_TOOL_LAUNCHER"] = "sandbox"
        else:
            env["BOBA_TOOL_LAUNCHER"] = "process"

        env["PYTHONUNBUFFERED"] = "1"
        env.pop("BOBA_URL_PREFIX", None)
        env.pop("BOBA_CONFIG_PATH", None)
        env.pop("KRB5_CLIENT_KTNAME", None)
        env.pop("KRB5CCNAME", None)

        return env


class StandPlugins:
    """Файлы conf/plugins приложения стенда рядом с его конфигом: загрузчик
    требует файл на каждый установленный плагин.

    Создаётся конфигом приложения на запись. У чата файлов плагинов нет:
    инструменты исполняет сервис boba-mcp; у сервиса без песочницы
    выключены инструменты, которым нужна она или внешние сервисы.
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

    def __init__(self, app: StandApp, sandbox: bool, extra: Sequence[str] = ()) -> None:
        self._app = app
        self._sandbox = sandbox
        self._extra = frozenset(extra)

    def copy_to(self, target: Path) -> None:
        source = self._app.files().plugins_dir()
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.glob("*.toml")):
            with path.open("rb") as handle:
                doc: dict[str, Any] = tomllib.load(handle)

            if not self._enabled(path.stem):
                doc["enable"] = False

            (target / path.name).write_text(TomlText.dumps(doc), encoding="utf-8")

    def _enabled(self, plugin: str) -> bool:
        if plugin in self._extra:
            return True

        if self._sandbox:
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
            "args": ["acp", "--setting-sources", ""],
        }
    )
    """Команда строкой оболочки и аргументы секции [agent.process] чата: по
    умолчанию агент boba.cli.app интерпретатором прогона; тест чужого агента
    ставит свою."""

    generation: str = ""
    """Поколение сессий: пустое — случайное на каждый старт процесса; общее у
    двух инстансов стенда даёт им принимать одну и ту же cookie входа."""

    service_plugins: tuple[str, ...] = ()
    """Плагины сервиса boba-mcp стенда сверх набора без песочницы."""
    agent_auth: StandAgentAuth = StandAgentAuth.PROXY
    """Способ входа чата и его агента на сервис boba-mcp стенда."""
    service_token_ttl_sec: int = 300
    """Срок токена доступа сервиса стенда: коротким тест проверяет, что
    агент обновляет токен сам."""
    messaging: str = ""
    """Провайдер шины [messaging]: пусто — как в конфиге отладки (память
    процесса); postgres — шина в базе стенда, общая у двух инстансов."""
    app_root: str = ""
    """Корень приложения chainlit ([env].app_root): пусто — assets пакета, как
    в отладке; свой корень даёт стенду другой конфиг chainlit (язык)."""

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
        """Кладёт файлы конфигурации приложения с правками стенда в рабочий
        каталог."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        doc = self.app.document()

        self._use_fake_llm(doc)
        self._use_test_profiles(doc)
        self._use_test_database(doc)
        self._use_local_storage(doc)
        self._without_mcp_servers(doc)
        self._use_local_auth(doc)
        self._use_agent(doc)
        self._shrink_pools(doc)
        doc["session"]["generation"] = self.generation

        self.files.write(doc)
        StandPlugins(self.app, self.sandbox).copy_to(self.files.plugins)

    def _use_agent(self, doc: MutableMapping[str, Any]) -> None:
        """Агент ACP стенда чата: процесс и история по полям стенда,
        приватные каталоги в рабочем каталоге стенда; инструменты «всегда
        загружать» — как в конфиге приложения."""
        if "agent" not in doc:
            return

        tools = doc["agent"]["tools"]

        private_dir = self.workdir / "agents"
        private_dir.mkdir(parents=True, exist_ok=True)
        doc["postgres_history"]["dbname"] = self.db_name
        doc["agent"] = {
            "public_url": self.base_url,
            "seal_ttl_sec": 600,
            "question_timeout_sec": 600,
            "settings_table": {"cipher": "${keys.encryption}"},
            "process": {
                "command": self.agent_process["command"],
                "args": self.agent_process["args"],
                "private_dir": str(private_dir),
                "start_timeout_sec": 60,
                "request_timeout_sec": 60,
                "stop_timeout_sec": 10,
            },
            "history": dict(self.agent_history),
            "tools": tools,
        }

    @staticmethod
    def _shrink_pools(doc: MutableMapping[str, Any]) -> None:
        """Стенды живут по нескольку сразу: малые пулы не исчерпывают слоты Postgres."""
        pool = doc["postgres"]["pool"]
        pool["min_size"] = 1
        pool["max_size"] = 6

    def env(self) -> dict[str, str]:
        """Окружение процесса приложения: BOBA_-переопределения поверх файлов
        конфигурации, которые уходят аргументами --config и --site.

        BOBA_BASE обязателен: site-файл стенда пишется в рабочий каталог, и base,
        вычисленный из его расположения, указывал бы мимо дерева отладки.
        """
        data_dir = self.data_dir
        for name in ("workspace", "tool-logs", "dump", "krb"):
            (data_dir / name).mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["BOBA_BASE"] = str(self.app.base.under(REPO_ROOT))
        env["BOBA_DATA"] = str(data_dir)
        # BOBA_CGROUP_BASE наследуется от прогона: поддерево делегирует user-юнит
        env["BOBA_PORT"] = str(self.app_port)
        env["BOBA_INSTANCE_ID"] = f"stand{self.app_port}"
        env["BOBA_URL_PREFIX"] = self.url_prefix
        if self.messaging:
            env["BOBA_MESSAGING"] = self.messaging
        if self.app_root:
            env["BOBA_APP_ROOT"] = self.app_root
        env["PGGSSENCMODE"] = "disable"
        # способ запуска tools фиксируется стендом, а не окружением прогона
        if self.sandbox:
            env["BOBA_TOOL_LAUNCHER"] = "sandbox"
        else:
            env["BOBA_TOOL_LAUNCHER"] = "process"
        # лог стенда читает упавший тест: буфер до kill не доживёт
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("KRB5_CLIENT_KTNAME", None)
        env.pop("KRB5CCNAME", None)
        return env

    def _use_fake_llm(self, doc: MutableMapping[str, Any]) -> None:
        """Провайдер стенда: fake llm процесса тестов вместо секций [llm] конфига."""
        doc["agent_http"] = {"dump": {"enable": False}}
        doc["model_limits"] = {
            "fake": {
                "context_window": 200000,
                "max_output_tokens": 4096,
                "reasoning_return": "turn",
            }
        }
        doc["llm"] = {
            "fake": {
                "kind": "openai",
                "transport": "${agent_http}",
                "connection": {
                    "scheme": "http",
                    "host": StandUrl.HOST,
                    "port": self.llm_port,
                    "path": "/v1",
                    "ssl_verify": False,
                    "auth": {"method": "bearer", "token": "none"},
                },
            }
        }

    def _use_test_profiles(self, doc: MutableMapping[str, Any]) -> None:
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

        doc["roles"] = {
            "ADM": {"tools": ["*"]},
            "DEV": {"tools": own},
            "GST": {"tools": []},
        }

        doc["profiles"] = {
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
            doc["profiles"] = {"general": doc["profiles"]["general"]}

    def _search_tools(self) -> list[str]:
        """Свои инструменты профиля search: у чата узкий набор отдаёт endpoint
        сервиса, у сервиса — его собственные инструменты."""
        if self.app is StandApp.AID:
            return []

        return ["diagram_save", "canvas_open"]

    def _use_test_database(self, doc: MutableMapping[str, Any]) -> None:
        """Сервер и учётка — из конфига приложения; стенду — отдельная база.

        Таблица соединений живёт там же: стенд сеет её после старта.
        """
        doc["postgres"]["dbname"] = self.db_name

    def _use_local_storage(self, doc: MutableMapping[str, Any]) -> None:
        """Файлы стенда: без песочницы — каталог на диске; с песочницей — образы
        workspace, как в бою: тела пишут в образ, и хост обязан читать его же."""
        files_dir = self.workdir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        if "storage" in doc and not self.sandbox:
            storage = doc["storage"]
            storage["kind"] = "local"
            storage["files_dir"] = str(files_dir)

        if journal := doc.get("stream_journal"):
            journal_dir = self.workdir / "tool-logs"
            journal_dir.mkdir(parents=True, exist_ok=True)
            journal["dir"] = str(journal_dir)

    def _without_mcp_servers(self, doc: MutableMapping[str, Any]) -> None:
        """Внешние MCP-серверы рабочего конфига стенду не нужны: набор
        инструментов сессии не должен зависеть от сети. Остаются только
        серверы, заданные самим стендом: их называет каждый профиль стенда."""
        service = self.service()
        servers: dict[str, dict[str, Any]] = {}
        own: dict[str, str] = {}
        if service is not None:
            for endpoint in service.ENDPOINTS:
                own[endpoint] = service.server_name(endpoint)
                servers[own[endpoint]] = service.server(endpoint)

        servers.update(self.mcp_servers)
        doc["mcp"] = {"servers": servers}
        for name, profile in doc["profiles"].items():
            named: list[str] = []
            if name in own:
                named.append(own[name])

            named.extend(sorted(self.mcp_servers))
            profile["mcp"] = named

    def _use_local_auth(self, doc: MutableMapping[str, Any]) -> None:
        """Учётки и роли стенда: рабочий [auth.local] не трогаем и не наследуем."""
        providers: list[str] = []

        if self.auth.sso:
            # keytab и SPN из рабочего конфига; маппинг ролей через LDAP стенду не нужен
            providers.append("${auth.kerberos}")
            doc["auth"]["kerberos"] = {
                "type": "kerberos",
                "principal_format": "{username}@${site.krb_realm}",
                "accept": {
                    "service_name": "HTTP/${site.krb_domain}@${site.krb_realm}",
                    "keytab": "${site.krb_http_keytab}",
                },
                "delegation": {
                    "mode": "constrained",
                    "service_ccache": "FILE:${site.krb_ccache_http}",
                    "krb5_config": "${site.krb_config}",
                },
            }
            if self.sso_roles:
                doc["auth"]["kerberos"]["roles"] = {
                    "principal": {"principal": dict(self.sso_roles)}
                }

        if self.auth.local:
            providers.append("${auth.local}")
            mapping = {login: list(roles) for login, roles in self.STAND_ROLES.items()}
            doc["auth"]["local"] = {
                "type": "local",
                "users": dict(self.STAND_USERS),
                "roles": {"local": {"mapping": mapping}},
                "require_roles": True,
            }

        doc["app"]["auth"] = providers


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
                "--config",
                str(self.config.files.config),
                "--site",
                str(self.config.files.site),
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
