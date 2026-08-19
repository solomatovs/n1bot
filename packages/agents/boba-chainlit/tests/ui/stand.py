"""Стенд ленты: конфиг приложения и запуск его отдельным процессом.

Приложение поднимается тем же входом, что и в проде, но провайдер модели, база,
хранилище и журнал уводятся на тестовые. Ошибки: StandError — стенд не поднялся
или не ответил в отведённое время.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from ui.toml_text import TomlText

__all__ = ["StandConfig", "StandError", "StandPaths", "StandProcess", "free_port"]

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
    llm_port: int
    db_name: str
    url_prefix: str = "/boba-test"
    single_profile: bool = False
    """True — в конфиге остаётся один профиль: селектора в UI быть не должно."""

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

    def credential(self, login: str = "") -> StandCredential:
        """Логин и пароль берутся из конфига разработчика, а не из кода.

        Без аргумента — первый логин по алфавиту; с аргументом — именно он.
        """
        base = StandPaths.BASE_CONFIG.under(REPO_ROOT)
        with base.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        try:
            users = doc["auth"]["local"]["users"]
        except KeyError as exc:
            msg = f"нет [auth.local].users в {base}"
            raise StandError(msg) from exc

        logins = sorted(users)
        if not logins:
            msg = f"в [auth.local].users пусто: {base}"
            raise StandError(msg)

        if not login:
            login = logins[0]

        if login not in users:
            msg = f"нет логина {login!r} в [auth.local].users: {base}"
            raise StandError(msg)

        return StandCredential(login=login, password=str(users[login]))

    def local_users(self) -> dict[str, list[str]]:
        """Логины [auth.local] рабочего конфига и их роли."""
        base = StandPaths.BASE_CONFIG.under(REPO_ROOT)
        with base.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        users = doc.get("auth", {}).get("local", {}).get("users", {})
        roles = doc.get("auth", {}).get("local", {}).get("roles", {})

        found: dict[str, list[str]] = {}
        for login in users:
            found[str(login)] = [str(r) for r in roles.get(login, [])]

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
        env["BOBA_PORT"] = str(self.app_port)
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
                "provider": "${openai.main}",
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
                "provider": "${openai.main}",
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
        """Сервер и учётка — из конфига приложения; стенду — отдельная база."""
        doc["postgres"]["dbname"] = self.db_name
        doc["connections"]["enable"] = False

    def _use_local_storage(self, doc: MutableMapping[str, Any]) -> None:
        files_dir = self.workdir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        storage = doc["storage"]
        storage["kind"] = "local"
        storage["files_dir"] = str(files_dir)

        journal_dir = self.workdir / "tool-logs"
        journal_dir.mkdir(parents=True, exist_ok=True)
        doc["stream_journal"]["dir"] = str(journal_dir)

    @staticmethod
    def _use_local_auth(doc: MutableMapping[str, Any]) -> None:
        doc["app"]["auth"] = ["${auth.local}"]

    def _disable_sandbox_tools(self, doc: MutableMapping[str, Any]) -> None:
        """Песочница стенду недоступна: остаются инструменты без bwrap."""
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


@dataclass
class StandProcess:
    """Дочерний процесс приложения: живёт на время сессии тестов."""

    config: StandConfig
    log_path: Path
    process: subprocess.Popen[bytes] | None = None

    def start(self, boot_timeout_sec: float) -> None:
        self.config.write()
        log = self.log_path.open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "boba.chainlit.main"],
            env=self.config.env(),
            cwd=str(REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self._await_ready(boot_timeout_sec)

    def stop(self) -> None:
        if self.process is None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def tail(self, lines: int = 40) -> str:
        """Хвост лога стенда: без него падение теста молчит о причине."""
        if not self.log_path.is_file():
            return ""

        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def _await_ready(self, timeout_sec: float) -> None:
        deadline = time.monotonic() + timeout_sec
        url = f"{self.config.base_url}/"
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise StandError(f"app exited early:\n{self.tail()}")

            try:
                response = httpx.get(url, timeout=2.0)
            except httpx.HTTPError:
                time.sleep(0.3)
                continue

            if response.status_code < 500:
                return

            time.sleep(0.3)

        raise StandError(f"app is not ready in {timeout_sec}s:\n{self.tail()}")


def free_port() -> int:
    """Свободный порт: параллельные прогоны не должны драться за один."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
