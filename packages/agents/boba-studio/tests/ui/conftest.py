"""Фикстуры ui-тестов studio: стенд studio, база, фейковый LLM, браузер."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api", reason="ui-тестам нужен playwright")

from studio_ui import BOOT_TIMEOUT_SEC

from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import serve
from boba.stand.ui.stand import StandApp, StandConfig, StandProcess, StandUrl, free_port

DB_NAME = "boba_ui_test"
TOKEN_DELAY_SEC = 0.03


@pytest.fixture(scope="session")
def stand_database() -> str:
    return StandDatabase(StandApp.STUDIO, DB_NAME).prepare()


@pytest.fixture(scope="session")
def stand_db(stand_database: str) -> StandDatabase:
    return StandDatabase(StandApp.STUDIO, stand_database)


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
            if httpx.get(StandUrl.of(port, "/v1/models"), timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.2)

    raise RuntimeError("fake llm did not start")


@pytest.fixture(scope="session")
def stand(
    stand_workdir: Path, llm_port: int, fake_llm: None, stand_database: str
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir,
        app=StandApp.STUDIO,
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
def workflow_stand(
    stand_workdir: Path, llm_port: int, fake_llm: None, stand_database: str
) -> Iterator[StandProcess]:
    """Стенд страницы workflow: запуски гонят bash, нужна песочница."""
    config = StandConfig(
        workdir=stand_workdir / "sandbox-workflow",
        app=StandApp.STUDIO,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-workflow",
        sandbox=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "workflow-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


