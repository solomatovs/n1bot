"""Стенд приложения: конфиг с правками стенда и запуск одного приложения отдельным
процессом тем же входом, что и в проде; провайдер модели, база, хранилище и журнал
уводятся на тестовые.

Ошибки:
StandError — стенд не поднялся или не ответил в отведённое время.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar

import httpx

from boba.stand.ui.toml_text import TomlText

__all__ = [
    "StandApp",
    "StandConfig",
    "StandError",
    "StandPaths",
    "StandProcess",
    "free_port",
]

REPO_ROOT = Path(__file__).resolve().parents[7]


class StandError(Exception):
    """Стенд не поднялся."""


class StandPaths(StrEnum):
    """Пути репозитория, которые стенд подставляет вместо рантайма релиза."""

    BASE_CONFIG = "compose/chainlit/conf/config.toml"
    STUDIO_BASE_CONFIG = "compose/studio/conf/config.toml"
    CHAINLIT_BASE = "compose/chainlit"
    STUDIO_BASE = "compose/studio"
    CHAINLIT_SANDBOX = "build/chainlit/src/sandbox"
    STUDIO_SANDBOX = "build/studio/src/sandbox"
    PACKAGES = "packages"

    def under(self, root: Path) -> Path:
        return root / self.value


class StandUrl(StrEnum):
    """Адрес локального стенда: сервер поднимает сам тест, TLS ему негде взять."""

    SCHEME = "http"
    HOST = "127.0.0.1"

    @classmethod
    def of(cls, port: int, path: str = "") -> str:
        return f"{cls.SCHEME}://{cls.HOST}:{port}{path}"


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


def free_port() -> int:
    """Свободный порт: параллельные прогоны не должны драться за один."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class StandCredential:
    """Учётка стенда: читается из [auth.local] рабочего конфига."""

    login: str
    password: str


class StandApp(StrEnum):
    """Приложения стенда: у каждого свой корень рантайма, конфиг, данные и порт."""

    CHAINLIT = "chainlit"
    STUDIO = "studio"

    @property
    def module(self) -> str:
        if self is StandApp.CHAINLIT:
            return "boba.chainlit.main"

        return "boba.studio"

    @property
    def base(self) -> StandPaths:
        if self is StandApp.CHAINLIT:
            return StandPaths.CHAINLIT_BASE

        return StandPaths.STUDIO_BASE

    @property
    def base_config(self) -> StandPaths:
        if self is StandApp.CHAINLIT:
            return StandPaths.BASE_CONFIG

        return StandPaths.STUDIO_BASE_CONFIG

    @property
    def sandbox(self) -> StandPaths:
        """Артефакты песочницы из сборки этого приложения."""
        if self is StandApp.CHAINLIT:
            return StandPaths.CHAINLIT_SANDBOX

        return StandPaths.STUDIO_SANDBOX

    @property
    def cgroup_base(self) -> str:
        if self is StandApp.CHAINLIT:
            return "/sys/fs/cgroup/boba.slice/boba-sandbox"

        return "/sys/fs/cgroup/boba.slice/boba-sandbox-studio"

    @property
    def ready_path(self) -> str:
        """Путь готовности под префиксом приложения."""
        if self is StandApp.CHAINLIT:
            return "/"

        return "/api/openapi.json"

    @property
    def data_layer_section(self) -> str:
        """Секция конфига со схемой хранения приложения."""
        if self is StandApp.CHAINLIT:
            return "data_layer"

        return "automation"


@dataclass
class StandConfig:
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

    SANDBOXED_TOOLS: ClassVar[tuple[str, ...]] = (
        "bash",
        "doc",
        "chart",
        "web",
        "confluence",
        "workflow",
        "ingest",
        "kb",
        "pg",
        "ch",
    )
    """Инструменты, которым нужна песочница или внешние сервисы."""

    auth: StandAuth = StandAuth.LOCAL
    """Набор провайдеров входа стенда."""

    sso_roles: dict[str, list[str]] = field(default_factory=dict)
    """Роли SSO-входа по принципалу: без них вход отклоняется как безролевой."""

    @property
    def config_path(self) -> Path:
        return self.workdir / "config.toml"

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
    }
    """Учётки стенда: фиксированы кодом, рабочий конфиг их не задаёт."""

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

    def write(self) -> Path:
        """Кладёт конфиг приложения с правками стенда в рабочий каталог."""
        self.workdir.mkdir(parents=True, exist_ok=True)
        base = self.app.base_config.under(REPO_ROOT)
        with base.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        self._use_fake_llm(doc)
        self._use_test_profiles(doc)
        self._use_catalog_roles(doc)
        self._use_test_database(doc)
        self._use_local_storage(doc)
        self._use_local_auth(doc)
        self._use_studio(doc)
        self._use_sandbox_artifacts(doc)
        self._shrink_pools(doc)

        self.config_path.write_text(TomlText.dumps(doc), encoding="utf-8")
        self._copy_plugins()
        return self.config_path

    def _copy_plugins(self) -> None:
        """Файлы conf/plugins рядом с конфигом стенда: загрузчик требует файл
        для каждого установленного плагина. Без песочницы остаются инструменты,
        которым она не нужна."""
        source = self.app.base_config.under(REPO_ROOT).parent / "plugins"
        target = self.workdir / "plugins"
        target.mkdir(parents=True, exist_ok=True)

        for path in sorted(source.glob("*.toml")):
            with path.open("rb") as handle:
                doc: dict[str, Any] = tomllib.load(handle)

            if not self.sandbox and path.stem in self.SANDBOXED_TOOLS:
                doc["enable"] = False

            (target / path.name).write_text(TomlText.dumps(doc), encoding="utf-8")

    @staticmethod
    def _shrink_pools(doc: MutableMapping[str, Any]) -> None:
        """Стенды живут по нескольку сразу: малые пулы не исчерпывают слоты Postgres."""
        pool = doc["postgres"]["pool"]
        pool["min_size"] = 1
        pool["max_size"] = 6

    def env(self) -> dict[str, str]:
        """Окружение процесса приложения: BOBA_-переопределения поверх конфига,
        который уходит аргументом --config.

        BOBA_BASE обязателен: конфиг стенда пишется в рабочий каталог, и base,
        вычисленный из его расположения, указывал бы мимо развёртывания.
        """
        data_dir = self.data_dir
        for name in ("workspace", "tool-logs", "dump", "krb"):
            (data_dir / name).mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["BOBA_BASE"] = str(self.app.base.under(REPO_ROOT))
        env["BOBA_DATA"] = str(data_dir)
        env["BOBA_CGROUP_BASE"] = self.app.cgroup_base
        env["BOBA_PORT"] = str(self.app_port)
        env["BOBA_INSTANCE_ID"] = f"stand{self.app_port}"
        env["BOBA_URL_PREFIX"] = self.url_prefix
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

    @staticmethod
    def _use_catalog_roles(doc: MutableMapping[str, Any]) -> None:
        """Каталог стенда: DEV читает, ADM правит; без секции ничего не меняется."""
        if "catalog" not in doc:
            return

        doc["catalog"]["view_roles"] = ["DEV"]
        doc["catalog"]["edit_roles"] = ["ADM"]

    def _use_fake_llm(self, doc: MutableMapping[str, Any]) -> None:
        """Транспорт стенда: поведение общее, адрес фейка — в профилях."""
        doc["http"] = {"ssl_verify": False, "dump": {"enable": False}}

    def _use_test_profiles(self, doc: MutableMapping[str, Any]) -> None:
        """Профили и роли стенда: фиксированные, тесты знают их наизусть.

        general — все инструменты, search — узкий набор; DEV-роль не покрывает
        canvas_open, чтобы было видно пересечение роли и профиля. Сценарий
        fake llm выбирается по тексту сообщения, поэтому имя модели свободно.
        """
        doc["roles"] = {
            "ADM": {"tools": ["*"]},
            "DEV": {
                "tools": [
                    "diagram_save",
                    "send_file",
                    "stream_logs_usage",
                    "stream_logs_cleanup",
                ]
            },
            "GST": {"tools": []},
        }

        doc["profiles"] = {
            "general": {
                "display_name": "General",
                "description": "Stand profile with every tool",
                "default": True,
                "roles": ["*"],
                "tools": ["*"],
                "provider": {
                    "kind": "openai",
                    "http": "${http}",
                    "base_url": StandUrl.of(self.llm_port, "/v1"),
                    "api_key": "none",
                },
                "model": "fake-model-general",
                "settings": ["*"],
                "system_prompt": "You are the general stand assistant",
                "history_messages": 30,
                "sampling": {
                    "temperature": 0.1,
                    "max_completion_tokens": 1111,
                    "top_p": 0.9,
                },
            },
            "search": {
                "display_name": "Search",
                "description": "Stand profile with a narrow toolset",
                "default": False,
                "roles": ["*"],
                "tools": ["diagram_save", "canvas_open"],
                "provider": {
                    "kind": "openai",
                    "http": "${http}",
                    "base_url": StandUrl.of(self.llm_port, "/v1"),
                    "api_key": "none",
                },
                "model": "fake-model-search",
                "settings": ["user_prompt"],
                "system_prompt": "You are the search stand assistant",
                "history_messages": 30,
                "sampling": {
                    "temperature": 0.7,
                    "max_completion_tokens": 2222,
                },
            },
        }

        if self.single_profile:
            doc["profiles"] = {"general": doc["profiles"]["general"]}

    def _use_test_database(self, doc: MutableMapping[str, Any]) -> None:
        """Сервер и учётка — из конфига приложения; стенду — отдельная база.

        Таблица соединений живёт там же: стенд сеет её после старта.
        """
        doc["postgres"]["dbname"] = self.db_name

    def _use_local_storage(self, doc: MutableMapping[str, Any]) -> None:
        files_dir = self.workdir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        if "storage" in doc:
            storage = doc["storage"]
            storage["kind"] = "local"
            storage["files_dir"] = str(files_dir)

        journal_dir = self.workdir / "tool-logs"
        journal_dir.mkdir(parents=True, exist_ok=True)
        doc["stream_journal"]["dir"] = str(journal_dir)

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
                doc["auth"]["kerberos"]["roles"] = {"principal": dict(self.sso_roles)}

        if self.auth.local:
            providers.append("${auth.local}")
            doc["auth"]["local"] = {
                "type": "local",
                "users": dict(self.STAND_USERS),
                "roles": {
                    login: list(roles) for login, roles in self.STAND_ROLES.items()
                },
                "require_roles": True,
            }

        doc["app"]["auth"] = providers

    @staticmethod
    def _use_studio(doc: MutableMapping[str, Any]) -> None:
        """Студия стенда: сборка страницы из dist конфига, слушает все адреса хоста."""
        if "studio" not in doc:
            return

        # SSO-тесты ходят доменным именем площадки, оно резолвится в адрес хоста
        doc["studio"]["host"] = "0.0.0.0"  # noqa: S104 — стенд, а не прод  # nosec B104
        doc["studio"]["page"] = "built"

    def _use_sandbox_artifacts(self, doc: MutableMapping[str, Any]) -> None:
        """Стенд с песочницей берёт rootfs плагинов и workspace.ext4 из сборки."""
        if not self.sandbox:
            return

        env = doc["env"]
        env["sandbox"] = str(self.app.sandbox.under(REPO_ROOT))


@dataclass
class StandProcess:
    """Процесс приложения стенда: живёт на время сессии тестов."""

    config: StandConfig
    log_path: Path
    process: subprocess.Popen[bytes] | None = None

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
                str(self.config.config_path),
            ],
            env=self.config.env(),
            cwd=str(REPO_ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
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
