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
    TOOLS = "build/src/sandbox/site"
    PACKAGES = "packages"

    def under(self, root: Path) -> Path:
        return root / self.value


class StandUser(StrEnum):
    """Учётка стенда: она же лежит в [auth.local] рабочего конфига."""

    NAME = "admin"
    PASSWORD = "myPassdfd3"


@dataclass
class StandConfig:
    """Конфиг стенда: пишет свой config.toml и env для дочернего процесса."""

    workdir: Path
    app_port: int
    llm_port: int
    db_port: int
    db_name: str
    url_prefix: str = "/boba-test"

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
        return f"http://127.0.0.1:{self.app_port}{self.url_prefix}"

    def write(self) -> Path:
        """Кладёт конфиг стенда в рабочий каталог и отдаёт его путь."""
        base = StandPaths.BASE_CONFIG.under(REPO_ROOT)
        with base.open("rb") as handle:
            doc: dict[str, Any] = tomllib.load(handle)

        self._use_fake_llm(doc)
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
        env["BOBA_TOOLS"] = str(StandPaths.TOOLS.under(REPO_ROOT))
        env["BOBA_ASSETS"] = str(StandPaths.ASSETS.under(REPO_ROOT))
        env["BOBA_BIND_CODE"] = f"{StandPaths.PACKAGES.under(REPO_ROOT)}:/opt/src"
        env["BOBA_SANDBOX_PYTHONPATH"] = "/opt/site"
        env["BOBA_CGROUP_BASE"] = "/sys/fs/cgroup/boba.slice"
        env["BOBA_PORT"] = str(self.app_port)
        env["BOBA_URL_PREFIX"] = self.url_prefix
        env["PGGSSENCMODE"] = "disable"
        # лог стенда читает упавший тест: буфер до kill не доживёт
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("KRB5_CLIENT_KTNAME", None)
        env.pop("KRB5CCNAME", None)
        return env

    def _use_fake_llm(self, doc: MutableMapping[str, Any]) -> None:
        doc["openai"]["fake"] = {
            "base_url": f"http://127.0.0.1:{self.llm_port}/v1",
            "api_key": "none",
            "ssl_verify": False,
            "dump": {"enable": False},
        }
        agent = doc["agent"]
        agent["openai"] = "${openai.fake}"
        agent["model"] = "fake-model"
        agent["temperature"] = 0.0

    def _use_test_database(self, doc: MutableMapping[str, Any]) -> None:
        postgres = doc["postgres"]
        postgres["host"] = "127.0.0.1"
        postgres["port"] = self.db_port
        postgres["user"] = "postgres"
        postgres["dbname"] = self.db_name
        postgres["gssencmode"] = "disable"
        postgres["sslmode"] = "disable"
        postgres.pop("hostaddr", None)
        postgres.pop("kerberos", None)
        # секция clickhouse ссылается на принципала postgres — уходит следом
        doc.get("clickhouse", {}).pop("kerberos", None)
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
