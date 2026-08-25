"""Фикстуры браузерных тестов ленты: стенд, авторизация, вкладка с журналом."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
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
    Playwright,
    ViewportSize,
    WebSocket,
    sync_playwright,
)
from psycopg import sql
from psycopg.errors import InsufficientPrivilege

from boba.chainlit.connections import (
    ConnectionsConfig,
    ConnectionStore,
    GrantTarget,
)
from boba.chainlit.infra.config import AppConfig
from boba.db.clickhouse import ClickHouseConfig
from boba.db.postgres import AsyncPostgresPool
from boba.settings import bind, build_app_config
from boba.transport.http import HttpProfile
from ui.chat_page import ChatPage
from ui.fake_llm import FakeRoute, serve
from ui.socket_log import SocketLog
from ui.stand import (
    REPO_ROOT,
    StandConfig,
    StandError,
    StandPaths,
    StandProcess,
    StandUrl,
    free_port,
)

DB_NAME = "boba_ui_test"
TOKEN_DELAY_SEC = 0.03
BOOT_TIMEOUT_SEC = 120.0


class StandExtension(StrEnum):
    """Расширения базы стенда: без них приложение не поднимается.

    pgvector регистрируется хуком configure на каждом соединении пула KB, то
    есть до первой миграции: без расширения соединение бракуется, и пул отдаёт
    PoolTimeout вместо внятной причины.
    """

    VECTOR = "vector"
    PG_TRGM = "pg_trgm"
    UNACCENT = "unaccent"
    BTREE_GIN = "btree_gin"

    def statement(self) -> sql.Composed:
        return sql.SQL("create extension if not exists {}").format(
            sql.Identifier(self.value)
        )

    def manual_hint(self, database: str) -> str:
        return (
            f"stand database {database} has no {self.value} extension "
            f"and the application role may not create it; "
            f"run as a superuser: "
            f"psql -d {database} -c 'create extension {self.value}'"
        )


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

    await _ensure_extensions(app_config, name)
    await _drop_connections(built, app_config, name)


async def _drop_connections(built: Any, app_config: AppConfig, name: str) -> None:
    """Сносит таблицы соединений: база стенда переживает прогоны, а их DDL
    меняется — приложение пересоздаёт таблицы на старте по текущей схеме."""
    connections = bind(built, path="connections", model=ConnectionsConfig)
    postgres = app_config.data_layer.postgres.model_copy(update={"dbname": name})
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        async with pool.cursor() as cur:
            for table in (connections.grants_table, connections.table):
                await cur.execute(
                    sql.SQL("drop table if exists {} cascade").format(
                        sql.Identifier(connections.db_schema, table)
                    )
                )
    finally:
        await pool.close()


async def _ensure_extensions(app_config: AppConfig, name: str) -> None:
    """Доводит базу стенда до расширений, которые приложение считает данностью."""
    postgres = app_config.data_layer.postgres.model_copy(update={"dbname": name})
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        async with pool.cursor() as cur:
            for extension in StandExtension:
                await cur.execute(
                    "select 1 from pg_extension where extname = %s",
                    (extension.value,),
                )
                installed = await cur.fetchone()
                if installed:
                    continue

                try:
                    await cur.execute(extension.statement())
                except InsufficientPrivilege as exc:
                    raise StandError(extension.manual_hint(name)) from exc
    finally:
        await pool.close()


def run_blocking(work: Coroutine[Any, Any, Any]) -> Any:
    """Коротина в своём потоке: в главном loop занят sync-playwright."""
    with ThreadPoolExecutor(max_workers=1) as runner:
        return runner.submit(asyncio.run, work).result()


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

        self._built = built
        pool = app_config.data_layer.postgres.pool.model_copy(update=self.POOL_OVERRIDE)
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

    def elements_named(self, name: str) -> int:
        """Сколько элементов с таким именем записал data layer стенда."""
        query = sql.SQL("select count(*) from {}.elements where name = %s").format(
            sql.Identifier(self._schema)
        )

        row = self._run(self._execute(query, (name,)))
        if row is None:
            return 0

        return int(row[0])

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

    def seed_connections(self, llm_port: int) -> None:
        """Соединения инструментов стенда: сервисные pg/ch под именем main и
        web-профиль фейкового сервера, выданные всем ролям стенда.

        Таблица чистится перед посевом: база стенда переживает прогоны.
        Роли в таблице появляются на старте приложения — сеять после него.
        """
        self._run(self._seed_connections(llm_port))

    async def _seed_connections(self, llm_port: int) -> None:
        connections = bind(self._built, path="connections", model=ConnectionsConfig)
        clickhouse = bind(self._built, path="clickhouse", model=ClickHouseConfig)
        web = HttpProfile(base_url=StandUrl.of(llm_port), ssl_verify=False)

        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            store = ConnectionStore(connections, pool)

            # строки прошлых прогонов могут не проходить нынешний валидатор
            # профиля, поэтому чистятся мимо стора
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL("delete from {}").format(
                        sql.Identifier(connections.db_schema, connections.grants_table)
                    )
                )
                await cur.execute(
                    sql.SQL("delete from {}").format(
                        sql.Identifier(connections.db_schema, connections.table)
                    )
                )

            roles = await store.roles()
            targets: list[GrantTarget] = []
            for role_names in StandConfig.STAND_ROLES.values():
                for role in role_names:
                    targets.append(GrantTarget.role(roles[role]))

            rows = [
                await store.add("main", self._postgres),
                await store.add("main", clickhouse),
                await store.add("stand", web),
            ]
            for connection_id in rows:
                for target in targets:
                    await store.grant(connection_id, target)
        finally:
            await pool.close()

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
        return run_blocking(work)


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
    url = StandUrl.of(port, FakeRoute.HEALTH.value)
    for _ in range(100):
        try:
            response = httpx.get(url, timeout=1.0)
        except httpx.HTTPError:
            time.sleep(0.1)
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
def playwright() -> Iterator[Playwright]:
    """Один sync-playwright на сессию.

    Sync API держит запущенный asyncio-loop в главном потоке, пока жив:
    второй sync_playwright() и asyncio.run() в этом потоке падают. Браузеры
    с другими аргументами поднимаются из этого же экземпляра.
    """
    with sync_playwright() as instance:
        yield instance


@pytest.fixture(scope="session")
def browser(playwright: Playwright) -> Iterator[Browser]:
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
    httpx.post(StandUrl.of(llm_port, FakeRoute.RESET.value), timeout=5.0)
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


@dataclass
class ChatOpener:
    """Открывает вкладки чата и закрывает их разом: свой стенд и логин на каждую."""

    browser: Browser
    llm_port: int
    contexts: list[BrowserContext] = field(default_factory=list)

    VIEWPORT: ClassVar[ViewportSize] = {"width": 1280, "height": 900}

    def open(self, stand: StandProcess, login: str = "") -> ChatPage:
        httpx.post(StandUrl.of(self.llm_port, FakeRoute.RESET.value), timeout=5.0)
        context = self.browser.new_context(viewport=self.VIEWPORT)
        self.contexts.append(context)
        context.add_cookies(login_cookies(stand, login))
        page: Page = context.new_page()
        log = SocketLog()
        _watch_sockets(page, log)
        chat_page = ChatPage(page=page, log=log, base_url=stand.config.base_url)
        chat_page.open()
        return chat_page

    def close(self) -> None:
        for context in self.contexts:
            context.close()

        self.contexts.clear()


OpenChat = Callable[[StandProcess, str], ChatPage]


@pytest.fixture
def open_chat(browser: Browser, llm_port: int) -> Iterator[OpenChat]:
    """Фабрика вкладок теста: роли и одиночный профиль открывают свои."""
    opener = ChatOpener(browser=browser, llm_port=llm_port)
    try:
        yield opener.open
    finally:
        opener.close()


@pytest.fixture(scope="module")
def module_chats(browser: Browser, llm_port: int) -> Iterator[ChatOpener]:
    """Вкладки на модуль: фикстуры-подготовки держат чат дольше одного теста."""
    opener = ChatOpener(browser=browser, llm_port=llm_port)
    try:
        yield opener
    finally:
        opener.close()


def _watch_sockets(page: Page, log: SocketLog) -> None:
    def on_socket(socket: WebSocket) -> None:
        socket.on("framereceived", log.accept)

    page.on("websocket", on_socket)
