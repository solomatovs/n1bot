"""Стенд ленты: конфиг приложения и запуск chainlit и studio отдельными процессами.

Процессы поднимаются теми же входами, что и в проде, за общим фронтом (ui.front);
провайдер модели, база, хранилище и журнал уводятся на тестовые.

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
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ui.front import FrontDoor, FrontRoutes
from ui.toml_text import TomlText

__all__ = [
    "StandConfig",
    "StandError",
    "StandLog",
    "StandPaths",
    "StandProcess",
    "free_port",
]

REPO_ROOT = Path(__file__).resolve().parents[5]


class StandError(Exception):
    """Стенд не поднялся."""


class StandPaths(StrEnum):
    """Пути репозитория, которые стенд подставляет вместо рантайма релиза."""

    BASE_CONFIG = "compose/conf/config.toml"
    ASSETS = "compose/chainlit"
    SANDBOX = "build/src/sandbox"
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


@dataclass
class StandConfig:
    """Конфиг стенда: пишет свой config.toml и env для дочернего процесса."""

    workdir: Path
    app_port: int
    """Порт фронта стенда: единственный адрес браузера и тестов."""

    llm_port: int
    db_name: str
    url_prefix: str = "/boba-test"
    chainlit_port: int = field(default_factory=free_port)
    studio_port: int = field(default_factory=free_port)
    single_profile: bool = False
    """True — в конфиге остаётся один профиль: селектора в UI быть не должно."""

    sandbox: bool = False
    """True — инструменты песочницы остаются включёнными: боевой путь целиком."""

    auth: StandAuth = StandAuth.LOCAL
    """Набор провайдеров входа стенда."""

    sso_roles: dict[str, list[str]] = field(default_factory=dict)
    """Роли SSO-входа по принципалу: без них вход отклоняется как безролевой."""

    SANDBOXED_TOOLS: tuple[str, ...] = (
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

    @property
    def config_path(self) -> Path:
        return self.workdir / "config.toml"

    @property
    def base_url(self) -> str:
        return StandUrl.of(self.app_port, self.url_prefix)

    STAND_USERS: ClassVar[dict[str, str]] = {
        "admin": "stand-admin-pass",
        "dev": "stand-dev-pass",
    }
    """Учётки стенда: фиксированы кодом, рабочий конфиг их не задаёт."""

    STAND_ROLES: ClassVar[dict[str, list[str]]] = {
        "admin": ["ADM"],
        "dev": ["DEV"],
    }
    """Роли учёток стенда: согласованы с [roles] стенда, а не рабочего конфига."""

    def credential(self, login: str = "") -> StandCredential:
        """Логин и пароль стенда; без аргумента — первый логин по алфавиту."""
        logins = sorted(self.STAND_USERS)
        if not login:
            login = logins[0]

        if login not in self.STAND_USERS:
            msg = f"нет логина {login!r} среди учёток стенда: {logins}"
            raise StandError(msg)

        return StandCredential(login=login, password=self.STAND_USERS[login])

    def local_users(self) -> dict[str, list[str]]:
        """Логины стенда и их роли."""
        found: dict[str, list[str]] = {}
        for login in self.STAND_USERS:
            found[login] = list(self.STAND_ROLES.get(login, []))

        return found

    def write(self) -> Path:
        """Кладёт конфиг стенда в рабочий каталог и отдаёт его путь."""
        base = StandPaths.BASE_CONFIG.under(REPO_ROOT)
        with base.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        self._use_fake_llm(doc)
        self._use_test_profiles(doc)
        self._use_test_database(doc)
        self._use_local_storage(doc)
        self._use_local_auth(doc)
        self._use_studio(doc)
        self._disable_sandbox_tools(doc)
        self._drop_cgroup_limits(doc)

        self.workdir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(TomlText.dumps(doc), encoding="utf-8")
        return self.config_path

    def env(self) -> dict[str, str]:
        """Окружение дочернего процесса: пути стенда вместо рантайма релиза."""
        env = dict(os.environ)
        env["BOBA_CONFIG_PATH"] = str(self.config_path)
        env["BOBA_RUNTIME"] = str(self.workdir)
        env["BOBA_SANDBOX"] = str(StandPaths.SANDBOX.under(REPO_ROOT))
        env["BOBA_ASSETS"] = str(StandPaths.ASSETS.under(REPO_ROOT))
        env["BOBA_BIND_CODE"] = f"{StandPaths.PACKAGES.under(REPO_ROOT)}:/opt/src"
        env["BOBA_SANDBOX_PYTHONPATH"] = "/opt/site"
        env["BOBA_CGROUP_BASE"] = "/sys/fs/cgroup/boba"
        env["BOBA_PORT"] = str(self.chainlit_port)
        env["BOBA_STUDIO_PORT"] = str(self.studio_port)
        env["BOBA_URL_PREFIX"] = self.url_prefix
        env["PGGSSENCMODE"] = "disable"
        # лог стенда читает упавший тест: буфер до kill не доживёт
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("KRB5_CLIENT_KTNAME", None)
        env.pop("KRB5CCNAME", None)
        return env

    def _use_fake_llm(self, doc: MutableMapping[str, Any]) -> None:
        doc["openai"] = {
            "main": {
                "base_url": StandUrl.of(self.llm_port, "/v1"),
                "api_key": "none",
                "ssl_verify": False,
                "dump": {"enable": False},
            }
        }

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
        }

        doc["settings"] = {
            "temperature": {"min": 0.0, "max": 2.0, "step": 0.05, "default": 1.0},
            "top_p": {"min": 0.0, "max": 1.0, "step": 0.05, "default": 1.0},
            "max_tokens": {"min": 256, "max": 16000, "step": 256, "default": 4096},
            "frequency_penalty": {
                "min": -2.0,
                "max": 2.0,
                "step": 0.1,
                "default": 0.0,
            },
            "presence_penalty": {
                "min": -2.0,
                "max": 2.0,
                "step": 0.1,
                "default": 0.0,
            },
            "history_messages": {"min": 1, "max": 100, "step": 1, "default": 30},
        }

        doc["profiles"] = {
            "general": {
                "display_name": "General",
                "description": "Stand profile with every tool",
                "default": True,
                "roles": ["*"],
                "tools": ["*"],
                "backend": {"provider": "openai", "openai": "${openai.main}"},
                "model": "fake-model-general",
                "models": ["fake-model-general", "fake-model-alt"],
                "settings": ["*"],
                "system_prompt": "You are the general stand assistant",
                "history_messages": 30,
                "temperature": 0.1,
                "max_tokens": 1111,
                "top_p": 0.9,
            },
            "search": {
                "display_name": "Search",
                "description": "Stand profile with a narrow toolset",
                "default": False,
                "roles": ["*"],
                "tools": ["diagram_save", "canvas_open"],
                "backend": {"provider": "openai", "openai": "${openai.main}"},
                "model": "fake-model-search",
                "models": ["fake-model-search"],
                "settings": ["temperature", "top_p", "history_messages", "user_prompt"],
                "system_prompt": "You are the search stand assistant",
                "history_messages": 30,
                "temperature": 0.7,
                "max_tokens": 2222,
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
        """Студия стенда: сборка страницы из dist конфига, слушает только loopback."""
        doc["studio"]["host"] = StandUrl.HOST.value
        doc["studio"]["page"] = "built"

    def _disable_sandbox_tools(self, doc: MutableMapping[str, Any]) -> None:
        """Без песочницы остаются инструменты, которым она не нужна."""
        if self.sandbox:
            return

        tools = doc.get("tool")
        if not isinstance(tools, Mapping):
            return

        for name in self.SANDBOXED_TOOLS:
            section = tools.get(name)
            if not isinstance(section, MutableMapping):
                continue

            section["enable"] = False

    @staticmethod
    def _drop_cgroup_limits(doc: MutableMapping[str, Any]) -> None:
        """Лимиты требуют делегированного поддерева — стенду его не дают.

        cgroup_base остаётся: он обязателен в модели профиля, а проба на старте
        включается только при заданных лимитах.
        """
        profiles = doc.get("sandbox", {}).get("profiles")
        if not isinstance(profiles, Mapping):
            return

        for profile in profiles.values():
            if not isinstance(profile, MutableMapping):
                continue

            for key in list(profile):
                if not key.startswith("cgroup_"):
                    continue

                if key == "cgroup_base":
                    continue

                del profile[key]


class StandLog(StrEnum):
    """Логи процессов стенда рядом с логом chainlit."""

    CHAINLIT = "chainlit"
    STUDIO = "studio"

    def path_of(self, log_path: Path) -> Path:
        if self is StandLog.CHAINLIT:
            return log_path

        return log_path.with_name(f"{log_path.stem}-{self.value}{log_path.suffix}")


@dataclass
class StandProcess:
    """Процессы стенда: chainlit и studio за общим фронтом; живут на время сессии."""

    config: StandConfig
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    studio: subprocess.Popen[bytes] | None = None
    front: FrontDoor | None = None

    COMPLAINT_MARKERS: ClassVar[tuple[str, ...]] = (
        "ERROR:",
        "Traceback",
        "Task exception was never retrieved",
        "loop mismatch",
    )
    """Маркеры строк, которых в логе живого хода быть не должно."""

    def start(self, boot_timeout_sec: float) -> None:
        self.config.write()
        env = self.config.env()
        self.process = self._spawn("boba.chainlit.main", env, StandLog.CHAINLIT)
        self.studio = self._spawn("boba.studio", env, StandLog.STUDIO)

        deadline = time.monotonic() + boot_timeout_sec
        prefix = self.config.url_prefix
        self._await_ready(
            self.process,
            StandUrl.of(self.config.chainlit_port, f"{prefix}/"),
            StandLog.CHAINLIT,
            deadline,
        )
        self._await_ready(
            self.studio,
            StandUrl.of(self.config.studio_port, f"{prefix}/api/openapi.json"),
            StandLog.STUDIO,
            deadline,
        )

        routes = FrontRoutes(prefix, self.config.chainlit_port, self.config.studio_port)
        self.front = FrontDoor(self.config.app_port, routes)
        self.front.start()

    def _spawn(
        self, module: str, env: Mapping[str, str], log: StandLog
    ) -> subprocess.Popen[bytes]:
        handle = log.path_of(self.log_path).open("wb")

        return subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", module],
            env=dict(env),
            cwd=str(REPO_ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        if self.front is not None:
            self.front.stop()

        for process in (self.process, self.studio):
            if process is None:
                continue

            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()

    def log_lines(self) -> int:
        """Сколько строк в логе chainlit сейчас: отметка начала хода."""
        if not self.log_path.is_file():
            return 0

        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return len(text.splitlines())

    def complaints(self, since_line: int = 0) -> list[str]:
        """Строки лога chainlit об ошибках начиная с отметки: ход обязан
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
        """Хвосты логов обоих процессов: без них падение теста молчит о причине."""
        parts: list[str] = []
        for log in StandLog:
            path = log.path_of(self.log_path)
            if not path.is_file():
                continue

            text = path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"== {log.value} ==")
            parts.append("\n".join(text.splitlines()[-lines:]))

        return "\n".join(parts)

    def _await_ready(
        self,
        process: subprocess.Popen[bytes],
        url: str,
        log: StandLog,
        deadline: float,
    ) -> None:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise StandError(f"{log.value} exited early:\n{self.tail()}")

            try:
                response = httpx.get(url, timeout=2.0)
            except httpx.HTTPError:
                time.sleep(0.3)
                continue

            if response.status_code < 500:
                return

            time.sleep(0.3)

        raise StandError(f"{log.value} did not answer at {url}:\n{self.tail()}")
