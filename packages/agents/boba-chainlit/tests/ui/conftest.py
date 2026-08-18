"""Фикстуры браузерных тестов ленты: стенд, авторизация, вкладка с журналом."""

from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

pytest.importorskip("playwright.sync_api", reason="ui-тестам нужен playwright")

from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    WebSocket,
    sync_playwright,
)
from psycopg import sql

from boba.chainlit.infra.config import AppConfig
from boba.db.postgres import AsyncPostgresPool
from boba.settings import bind, build_app_config
from ui.chat_page import ChatPage
from ui.fake_llm import serve
from ui.socket_log import SocketLog
from ui.stand import (
    REPO_ROOT,
    StandConfig,
    StandPaths,
    StandProcess,
    StandUrl,
    free_port,
)

DB_NAME = "boba_ui_test"
TOKEN_DELAY_SEC = 0.03
BOOT_TIMEOUT_SEC = 120.0


async def _ensure_database(name: str) -> None:
    """Создаёт базу стенда на сервере из конфига приложения (если её нет)."""
    built = build_app_config(config_path=StandPaths.BASE_CONFIG.under(REPO_ROOT))
    app_config = bind(built, path="app", model=AppConfig)

    maintenance = AsyncPostgresPool(app_config.data_layer.postgres)
    await maintenance.open()
    try:
        async with maintenance.cursor() as cur:
            await cur.execute(
                "select 1 from pg_database where datname = %s",
                (name,),
            )
            exists = await cur.fetchone()

            if not exists:
                await cur.execute(
                    sql.SQL("create database {}").format(sql.Identifier(name))
                )
    finally:
        await maintenance.close()


@pytest.fixture(scope="session")
def stand_database() -> str:
    # свой поток: у сессии pytest может быть уже запущенный event loop
    with ThreadPoolExecutor(max_workers=1) as runner:
        runner.submit(asyncio.run, _ensure_database(DB_NAME)).result()

    return DB_NAME


class StandDatabase:
    """Доступ к базе стенда: один пул на операцию, kerberos как у приложения.

    Пул приложения по конфигу поднимает несколько соединений и каждое проходит
    kinit — тесту хватает одного, иначе очередь упирается в таймаут.
    """

    POOL_OVERRIDE: ClassVar[dict[str, Any]] = {
        "min_size": 1,
        "max_size": 1,
        "timeout": 30.0,
    }

    def __init__(self, name: str) -> None:
        built = build_app_config(config_path=StandPaths.BASE_CONFIG.under(REPO_ROOT))
        app_config = bind(built, path="app", model=AppConfig)

        pool = app_config.data_layer.postgres.pool.model_copy(
            update=self.POOL_OVERRIDE
        )
        self._postgres = app_config.data_layer.postgres.model_copy(
            update={"dbname": name, "pool": pool}
        )
        self._schema = app_config.data_layer.db_schema

    def wipe_llm_settings(self) -> None:
        """Снимает сохранённые настройки LLM у всех пользователей базы."""
        query = sql.SQL("update {}.users set meta = meta - 'llm'").format(
            sql.Identifier(self._schema)
        )
        self._run(self._execute(query, None))

    def llm_settings_of(self, identifier: str) -> dict:
        """Ключ llm из users.meta: тест сверяет, что именно сохранилось."""
        query = sql.SQL(
            "select coalesce(meta -> 'llm', '{{}}'::jsonb) "
            "from {}.users where identifier = %s"
        ).format(sql.Identifier(self._schema))

        row = self._run(self._execute(query, (identifier,)))
        if row is None:
            raise RuntimeError(f"user {identifier} is not stored")

        return dict(row[0])

    async def _execute(
        self,
        query: sql.Composed,
        params: tuple[Any, ...] | None,
    ) -> Any:
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            async with pool.cursor() as cur:
                await cur.execute(query, params)
                if cur.description is None:
                    return None

                return await cur.fetchone()
        finally:
            await pool.close()

    @staticmethod
    def _run(work: Coroutine[Any, Any, Any]) -> Any:
        # свой поток: у сессии pytest может быть уже запущенный event loop
        with ThreadPoolExecutor(max_workers=1) as runner:
            return runner.submit(asyncio.run, work).result()


@pytest.fixture
def stand_db() -> StandDatabase:
    """Доступ к базе стенда; имя базы фиксировано на весь прогон."""
    return StandDatabase(DB_NAME)


@pytest.fixture
def clean_llm_settings(stand_db: StandDatabase) -> None:
    """Каждый тест панели начинает без чужих сохранённых настроек."""
    stand_db.wipe_llm_settings()


LlmMetaReader = Callable[[str], dict]


@pytest.fixture
def llm_meta(stand_db: StandDatabase) -> LlmMetaReader:
    """Читалка сохранённых настроек пользователя из базы стенда."""
    return stand_db.llm_settings_of


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры conftest: UI-тесты ходят через браузер."""


@pytest.fixture(scope="session")
def stand_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("boba-stand")


@pytest.fixture(scope="session")
def llm_port() -> int:
    return free_port()


@pytest.fixture(scope="session")
def fake_llm(llm_port: int) -> Iterator[None]:
    """Провайдер модели: отдельный процесс, чтобы не делить loop со стендом."""
    process = multiprocessing.Process(
        target=serve,
        args=("127.0.0.1", llm_port, TOKEN_DELAY_SEC),
        daemon=True,
    )
    process.start()
    _await_llm(llm_port)
    try:
        yield
    finally:
        process.terminate()
        process.join(timeout=10)


def _await_llm(port: int) -> None:
    url = StandUrl.of(port, "/health")
    for _ in range(100):
        try:
            response = httpx.get(url, timeout=1.0)
        except httpx.HTTPError:
            continue

        if response.status_code == 200:
            return

    raise RuntimeError(f"fake llm is not ready on {port}")


@pytest.fixture(scope="session")
def stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


def login_cookies(stand: StandProcess, login: str = "") -> list[SetCookieParam]:
    """Логин формой chainlit: тест ходит той же дорогой, что и пользователь."""
    credential = stand.config.credential(login)
    response = httpx.post(
        f"{stand.config.base_url}/login",
        data={
            "username": credential.login,
            "password": credential.password,
        },
        timeout=30.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"login failed: {response.status_code} {response.text[:200]}"
        )

    cookies: list[SetCookieParam] = []
    for name, value in response.cookies.items():
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": "127.0.0.1",
                "path": "/",
            }
        )

    if not cookies:
        raise RuntimeError("login returned no cookies")

    return cookies


@pytest.fixture(scope="session")
def auth_cookies(stand: StandProcess) -> list[SetCookieParam]:
    return login_cookies(stand)


@pytest.fixture(scope="session")
def solo_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    """Второй стенд с единственным профилем: селектора в UI быть не должно."""
    config = StandConfig(
        workdir=stand_workdir / "solo",
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-solo",
        single_profile=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "solo-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(args=["--no-sandbox"])
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def chat(
    browser: Browser,
    stand: StandProcess,
    auth_cookies: list[SetCookieParam],
    llm_port: int,
) -> Iterator[ChatPage]:
    httpx.post(StandUrl.of(llm_port, "/reset"), timeout=5.0)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_cookies(auth_cookies)
    page: Page = context.new_page()
    log = SocketLog()
    _watch_sockets(page, log)
    chat_page = ChatPage(page=page, log=log, base_url=stand.config.base_url)
    try:
        chat_page.open()
        yield chat_page
    finally:
        context.close()


OpenChat = Callable[[StandProcess, str], ChatPage]


@pytest.fixture
def open_chat(browser: Browser, llm_port: int) -> Iterator[OpenChat]:
    """Фабрика вкладок: свой стенд и логин на каждую (роли и одиночный профиль)."""
    contexts: list[BrowserContext] = []

    def factory(stand: StandProcess, login: str = "") -> ChatPage:
        httpx.post(StandUrl.of(llm_port, "/reset"), timeout=5.0)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        contexts.append(context)
        context.add_cookies(login_cookies(stand, login))
        page: Page = context.new_page()
        log = SocketLog()
        _watch_sockets(page, log)
        chat_page = ChatPage(page=page, log=log, base_url=stand.config.base_url)
        chat_page.open()
        return chat_page

    try:
        yield factory
    finally:
        for context in contexts:
            context.close()


def _watch_sockets(page: Page, log: SocketLog) -> None:
    def on_socket(socket: WebSocket) -> None:
        socket.on("framereceived", log.accept)

    page.on("websocket", on_socket)
