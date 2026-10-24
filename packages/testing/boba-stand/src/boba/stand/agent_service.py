"""Стенд службы агента (`boba serve`): в цикле событий теста и отдельным
процессом, вход в неё proxy доверенного клиента.

Ошибки:
ServiceSignInError — служба не выдала токен (boba.stand.service_signin).
AgentServiceProcessError — процесс службы не начал отвечать или вышел до этого.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import httpx
import uvicorn

from boba.cli.serve import ServeConfig, ServeHistory, ServeHost, ServeMark
from boba.core.secrets import SecretReveal
from boba.db.postgres.connection.auth import KeytabAuth
from boba.krb import KerberosWorkspaceConfig
from boba.stand.service_signin import ServiceSignIn
from boba.stand.ui.toml_text import TomlText

__all__ = [
    "AgentServiceProcess",
    "AgentServiceProcessError",
    "AgentServiceStand",
    "ServeAddress",
    "ServeStandConfig",
    "ServeThread",
    "ServiceProfiles",
]


@dataclass(frozen=True)
class ServeAddress:
    """Порты службы стенда: на каком она слушает и какой называет своим
    адресом (public_url, издатель токенов). Без прокси стенда они равны, за
    прокси (FaultProxy) публичный — порт прокси. Порты выбирает тест —
    StandPorts().free()."""

    listen: int
    public: int


class AgentServiceProcessError(Exception):
    """Процесс службы агента стенда не начал отвечать."""


class ServiceProfiles:
    """Профили агента стенда [agent.profiles]: роли и профиль модели над
    фейковой моделью по адресу base_url."""

    API_KEY: ClassVar[str] = "sk-agent-service-stand"

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._profiles: dict[str, dict[str, Any]] = {}

    def add(self, name: str, roles: Sequence[str], model: str) -> ServiceProfiles:
        self._profiles[name] = {
            "roles": list(roles),
            "model": {
                "provider": {
                    "kind": "openai",
                    "baseUrl": self._base_url,
                    "apiKey": self.API_KEY,
                },
                "model": model,
                "contextWindow": 32000,
                "maxOutputTokens": 4096,
            },
        }

        return self

    def section(self) -> dict[str, dict[str, Any]]:
        return dict(self._profiles)


class ServeStandConfig:
    """Конфиг службы агента стенда и её адрес: секция [app] словарём на
    свободном порту, вход proxy клиента CLIENT_ID под ключом PROXY_SECRET.

    Его берут служба в цикле теста (AgentServiceStand) и служба отдельным
    процессом (AgentServiceProcess). history — подключение истории (своя
    схема стенда), settings — слой настроек агента без истории и моделей
    (права, сессии), profiles — [agent.profiles], idle_sec — простой, после
    которого служба закрывает брошенное подключение HTTP, address — порты.
    """

    CLIENT_ID: ClassVar[str] = "stand-chat"
    CLIENT_SECRET: ClassVar[str] = "stand-agent-client-secret"  # noqa: S105 — ключ стенда
    PROXY_SECRET: ClassVar[str] = "stand-agent-proxy-secret"  # noqa: S105 — ключ стенда
    SESSION_SECRET: ClassVar[str] = "stand-agent-session-secret"  # noqa: S105 — ключ стенда
    PATH: ClassVar[str] = "/acp"
    PREFIX: ClassVar[str] = "/boba"
    """Префикс службы: стенд, как прод за прокси, отвечает под ним."""

    def __init__(  # noqa: PLR0913 — служба собирается всеми частями стенда
        self,
        krb: KerberosWorkspaceConfig,
        history: ServeHistory,
        settings: Mapping[str, Any],
        profiles: ServiceProfiles,
        idle_sec: float,
        address: ServeAddress,
    ) -> None:
        self.port = address.listen
        self.public_port = address.public
        self.public_url = f"http://127.0.0.1:{address.public}{self.PREFIX}"
        self._krb = krb
        self._history = history
        self._settings = dict(settings)
        self._profiles = profiles
        self._idle_sec = idle_sec
        self._sign_in = ServiceSignIn(
            self.public_url, self.CLIENT_ID, self.CLIENT_SECRET, self.PROXY_SECRET
        )

    def app(self) -> dict[str, Any]:
        """Секция [app] службы для ServeConfig в процессе теста: подключение
        истории — моделью, как его дал тест."""
        return self._app(self._history)

    def document(self) -> dict[str, Any]:
        """Секция [app] службы для файла конфига процесса: подключение истории
        словарём без пустых полей.

        Ошибки:
        AgentServiceProcessError — вход истории не по keytab: секреты другого
            входа в файл не пишутся.
        """
        return self._app(self._history_section())

    def _app(self, history: object) -> dict[str, Any]:
        return {
            "schema": "agent_service_stand",
            "krb": self._krb.model_dump(mode="json", exclude_none=True),
            "auth": [
                {
                    "type": "proxy",
                    "secret": self.PROXY_SECRET,
                    "roles": {"header": {}},
                }
            ],
            "session": {
                "auth_secret": self.SESSION_SECRET,
                "session_ttl_sec": 300,
                "session_max_sec": 3600,
                "generation": "stand",
            },
            "agent": {
                "host": "127.0.0.1",
                "port": self.public_port,
                "path": self.PATH,
                "public_url": self.public_url,
                "url_prefix": self.PREFIX,
                "shutdown_timeout_sec": 2,
                "idle_sec": self._idle_sec,
                "history": history,
                "settings": self._settings,
                "profiles": self._profiles.section(),
                "clients": {self.CLIENT_ID: {"secret": self.CLIENT_SECRET}},
            },
        }

    def url(self) -> str:
        return f"{self.public_url}{self.PATH}"

    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.PREFIX}{self.PATH}"

    async def token(self, login: str, roles: str) -> str:
        """Токен ресурса ACP по утверждению доверенного клиента."""
        return await asyncio.to_thread(self._sign_in.token, login, roles, self.url())

    def _history_section(self) -> dict[str, object]:
        """Путь keytab и принципал — не секреты, поэтому дамп идёт без контекста
        раскрытия, а голые SecretStr раскрываются обходом."""
        auth = self._history.postgres.auth
        if not isinstance(auth, KeytabAuth):
            got = type(auth).__name__
            msg = (
                "agent service process: the history of the stand must be entered "
                f"with a keytab to be written to the config file, got {got}"
            )
            raise AgentServiceProcessError(msg)

        dumped = self._history.model_dump(mode="json", by_alias=True, exclude_none=True)
        SecretReveal.apply(self._history, dumped)

        return dumped


class AgentServiceStand:
    """Служба агента в цикле теста: сессии — потоки этого же процесса, конфиг
    и вход — ServeStandConfig."""

    VERSION: ClassVar[str] = "0.0.0-stand"
    WAIT_SEC: ClassVar[float] = 60.0

    def __init__(  # noqa: PLR0913 — служба собирается всеми частями стенда
        self,
        krb: KerberosWorkspaceConfig,
        history: ServeHistory,
        settings: Mapping[str, Any],
        profiles: ServiceProfiles,
        idle_sec: float,
        address: ServeAddress,
    ) -> None:
        self._config = ServeStandConfig(
            krb, history, settings, profiles, idle_sec, address
        )
        self.port = self._config.port
        self.public_url = self._config.public_url
        config = ServeConfig.model_validate(self._config.app())
        self.host = ServeHost(config, self.VERSION)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    def url(self) -> str:
        return self._config.url()

    def ws_url(self) -> str:
        return self._config.ws_url()

    async def start(self) -> None:
        await self.host.start()
        server = uvicorn.Server(
            uvicorn.Config(
                self.host.app(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=2,
            )
        )
        self._server = server
        task = asyncio.create_task(server.serve())
        self._task = task
        while not server.started:
            if task.done():
                await task

            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

        if self._task is not None:
            await self._task

        await self.host.stop()

    async def token(self, login: str, roles: str) -> str:
        """Токен ресурса ACP по утверждению доверенного клиента."""
        return await self._config.token(login, roles)

    async def sessions_drop_to(self, wanted: int) -> None:
        """Ждёт, пока у службы останется wanted открытых сессий."""
        deadline = time.monotonic() + self.WAIT_SEC
        while True:
            opened = self.host.sessions()
            if opened == wanted:
                return

            if time.monotonic() >= deadline:
                msg = (
                    f"agent service stand: expected {wanted} open sessions "
                    f"within {self.WAIT_SEC}s, got {opened}"
                )
                raise AssertionError(msg)

            await asyncio.sleep(0.1)


class ServeFiles(StrEnum):
    """Файлы процесса службы стенда в его рабочем каталоге."""

    CONFIG = "config.toml"
    SITE = "conf/site.toml"
    LOG = "serve.log"

    def under(self, workdir: Path) -> Path:
        return workdir / self.value


class AgentServiceProcess:
    """Служба агента отдельным процессом `<command> serve --config --site`:
    модуль venv или собранный бинарник. Конфиг — ServeStandConfig, записанный
    в рабочий каталог; вывод процесса — в файл serve.log там же. Нужна тестам,
    которые останавливают службу сигналом и смотрят код выхода и журнал.
    """

    WAIT_SEC: ClassVar[float] = 60.0
    POLL_SEC: ClassVar[float] = 0.1
    STOP_SEC: ClassVar[float] = 30.0

    def __init__(
        self, command: Sequence[str], workdir: Path, config: ServeStandConfig
    ) -> None:
        self._command = list(command)
        self._workdir = workdir
        self._config = config
        self._process: subprocess.Popen[bytes] | None = None

    def url(self) -> str:
        return self._config.url()

    def ws_url(self) -> str:
        return self._config.ws_url()

    async def token(self, login: str, roles: str) -> str:
        return await self._config.token(login, roles)

    async def start(self) -> None:
        """Пишет конфиг, запускает процесс и ждёт ответа пробы готовности.

        Ошибки:
        AgentServiceProcessError — процесс вышел или не ответил за WAIT_SEC.
        """
        config = ServeFiles.CONFIG.under(self._workdir)
        site = ServeFiles.SITE.under(self._workdir)
        site.parent.mkdir(parents=True, exist_ok=True)
        document = TomlText.dumps({"app": self._config.document()})
        config.write_text(document, encoding="utf-8")

        empty_site = TomlText.dumps({"env": {}})
        site.write_text(empty_site, encoding="utf-8")
        argv = [*self._command, "serve", "--config", str(config), "--site", str(site)]
        with ServeFiles.LOG.under(self._workdir).open("wb") as log:
            self._process = subprocess.Popen(  # noqa: S603 — стенд теста
                argv, stdout=log, stderr=subprocess.STDOUT, cwd=self._workdir
            )

        await self._ready()

    async def terminate(self) -> int:
        """SIGTERM процессу и его код выхода."""
        process = self._running()
        process.send_signal(signal.SIGTERM)

        return await asyncio.to_thread(process.wait, self.STOP_SEC)

    async def kill(self) -> None:
        """Добивает процесс, если он ещё жив."""
        process = self._process
        if process is None:
            return

        if process.poll() is not None:
            return

        process.kill()
        await asyncio.to_thread(process.wait, self.STOP_SEC)

    def log(self) -> str:
        return ServeFiles.LOG.under(self._workdir).read_text(encoding="utf-8")

    def _running(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None:
            msg = "agent service process: start() must run before it is stopped"
            raise AgentServiceProcessError(msg)

        return process

    async def _ready(self) -> None:
        process = self._running()
        url = f"{self._config.public_url}{ServeMark.HEALTH.value}"
        deadline = time.monotonic() + self.WAIT_SEC
        async with httpx.AsyncClient() as client:
            while True:
                code = process.poll()
                if code is not None:
                    msg = (
                        f"agent service process {self._command}: expected it to "
                        f"answer {url}, it exited with {code}: {self.log()[-2000:]}"
                    )
                    raise AgentServiceProcessError(msg)

                if await self._answers(client, url):
                    return

                if time.monotonic() >= deadline:
                    msg = (
                        f"agent service process {self._command}: GET {url} did not "
                        f"answer 200 within {self.WAIT_SEC}s: {self.log()[-2000:]}"
                    )
                    raise AgentServiceProcessError(msg)

                await asyncio.sleep(self.POLL_SEC)

    @staticmethod
    async def _answers(client: httpx.AsyncClient, url: str) -> bool:
        try:
            reply = await client.get(url)
        except httpx.TransportError:
            return False

        return reply.status_code == httpx.codes.OK


class ServeThread:
    """Служба агента стенда в своём потоке и цикле событий — для синхронных
    ui- и look-тестов: start поднимает службу, собранную served, stop гасит
    её, и следующий start поднимает новую на тех же портах (перезапуск
    службы с потерей сессий в памяти)."""

    WAIT_SEC: ClassVar[float] = 120.0

    def __init__(self, served: Callable[[], AgentServiceStand]) -> None:
        self._served = served
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._failure: BaseException | None = None

    def start(self) -> None:
        """Ошибки:
        AgentServiceProcessError — служба не поднялась за WAIT_SEC.
        """
        self._ready.clear()
        self._failure = None
        self._thread = threading.Thread(target=self._run, name="serve", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.WAIT_SEC):
            msg = f"agent service stand: not started within {self.WAIT_SEC}s"
            raise AgentServiceProcessError(msg)

        if self._failure is not None:
            msg = f"agent service stand did not start: {self._failure}"
            raise AgentServiceProcessError(msg) from self._failure

    def stop(self) -> None:
        loop = self._loop
        stop = self._stop
        thread = self._thread
        if loop is None or stop is None or thread is None:
            return

        loop.call_soon_threadsafe(stop.set)
        thread.join(self.WAIT_SEC)
        self._thread = None

    def _run(self) -> None:
        asyncio.run(self._served_until_stop())

    async def _served_until_stop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        served = self._served()
        try:
            await served.start()
        except Exception as exc:
            self._failure = exc
            self._ready.set()
            return

        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await served.stop()
