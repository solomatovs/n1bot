"""Фикстуры ui-тестов чата: стенд chainlit, база, фейковый LLM, браузер и вкладки."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api", reason="ui-тестам нужен playwright")

from catalog_ui import Api, Ed, Seed, SourceSeed, api_client
from chat_ui import (
    BOOT_TIMEOUT_SEC,
    ChatOpener,
    LlmMetaReader,
    OpenChat,
    login_cookies,
    watch_sockets,
)
from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import Browser, Page

from boba.stand.ui.chat_page import ChatPage
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import FakeRoute, serve
from boba.stand.ui.socket_log import SocketLog
from boba.stand.ui.stand import StandApp, StandConfig, StandProcess, StandUrl, free_port

DB_NAME = "boba_ui_test"
TOKEN_DELAY_SEC = 0.03


@pytest.fixture(scope="session")
def stand_database() -> str:
    return StandDatabase(StandApp.CHAINLIT, DB_NAME).prepare()


@pytest.fixture(scope="session")
def stand_db(stand_database: str) -> StandDatabase:
    return StandDatabase(StandApp.CHAINLIT, stand_database)


@pytest.fixture
def clean_llm_settings(stand_db: StandDatabase) -> None:
    stand_db.wipe_llm_settings()


@pytest.fixture
def llm_meta(stand_db: StandDatabase) -> LlmMetaReader:
    return stand_db.llm_settings_of


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Стенд живёт отдельным процессом: сессия chainlit в тесте не нужна."""


@pytest.fixture(scope="session")
def stand_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("boba-stand")


@pytest.fixture(scope="session")
def llm_port() -> int:
    return free_port()


@pytest.fixture(scope="session")
def fake_llm(llm_port: int) -> Iterator[None]:
    process = multiprocessing.Process(
        target=serve, args=(StandUrl.HOST.value, llm_port, TOKEN_DELAY_SEC), daemon=True
    )
    process.start()
    try:
        _await_llm(llm_port)
        yield
    finally:
        process.terminate()
        process.join(timeout=10)


def _await_llm(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if (
                httpx.get(StandUrl.of(port, "/v1/models"), timeout=1.0).status_code
                < 500
            ):
                return
        except httpx.HTTPError:
            time.sleep(0.2)

    msg = f"fake llm did not answer GET {StandUrl.of(port, '/v1/models')} in 30s"
    raise RuntimeError(msg)


@pytest.fixture(scope="session")
def stand(
    stand_workdir: Path, llm_port: int, fake_llm: None, stand_database: str
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir,
        app=StandApp.CHAINLIT,
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


@pytest.fixture(scope="session")
def auth_cookies(stand: StandProcess) -> list[SetCookieParam]:
    return login_cookies(stand)


@pytest.fixture(scope="session")
def solo_stand(
    stand_workdir: Path, llm_port: int, fake_llm: None, stand_database: str
) -> Iterator[StandProcess]:
    """Второй стенд с единственным профилем: селектора в UI быть не должно."""
    config = StandConfig(
        workdir=stand_workdir / "solo",
        app=StandApp.CHAINLIT,
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
    watch_sockets(page, log)
    chat_page = ChatPage(page=page, log=log, base_url=stand.config.base_url)
    try:
        chat_page.open()
        yield chat_page
    finally:
        context.close()


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


@pytest.fixture(scope="module")
def catalog_api(stand: StandProcess, stand_db: StandDatabase) -> Iterator[Api]:
    """JSON API каталога от имени admin; один клиент на модуль. Подключения
    для источников сеятели кладут в базу стенда напрямую."""
    with api_client(stand, "admin") as admin:
        yield Api(admin, stand_db)


@pytest.fixture(scope="module")
def catalog_seed(catalog_api: Api) -> Iterator[Seed]:
    """Процесс модуля над источником ed_prod: публикуется на входе, на выходе
    снимаются его виды, публикуется удаление и удаляется источник, чтобы
    соседние модули видели прежний каталог."""
    seed = Seed(catalog_api)
    seed.publish("module seed")
    try:
        yield seed
    finally:
        for view in catalog_api.views():
            if str(view["name"]).startswith(Ed.PREFIX):
                catalog_api.delete_view(str(view["id"]))

        seed.cleanup()


@pytest.fixture(scope="module")
def source_seed(catalog_api: Api) -> Iterator[SourceSeed]:
    """Источники модуля: postgres с двумя версиями из образца, clickhouse с
    одной, ручной postgres без версий; на выходе удаляются."""
    seed = SourceSeed(catalog_api)
    try:
        yield seed
    finally:
        seed.cleanup()
