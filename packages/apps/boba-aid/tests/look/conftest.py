"""Фикстуры тестов вида aid: свои стенды aid (входы local, local+sso, sso) на
своей базе и своём FakeLLM, вкладки в теме и размере кадра, проверка
эталонов, окружения снимков и свежести сборки фронта, отчёт прогона."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api", reason="тестам вида нужен playwright")

from aid_look import (
    FrontBuild,
    LookCheck,
    LookEnvironments,
    LookError,
    LookPages,
    LookReport,
    LookSettings,
    LookStand,
)
from playwright.sync_api import Browser

from boba.stand.names import StandNames
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import FakeRoute, serve
from boba.stand.ui.stand import StandApp, StandUrl, free_port


class LookOption(StrEnum):
    """Имена опций прогона вида (их объявляет packages/conftest.py)."""

    UPDATE = "look_update"
    OUT = "look_out"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Стенд живёт отдельным процессом: сессия chainlit в тесте не нужна."""


@pytest.fixture(scope="session")
def look_settings(request: pytest.FixtureRequest) -> LookSettings:
    out = str(request.config.getoption(LookOption.OUT.value))
    root = request.config.rootpath / "debug" / "look-out"
    if out:
        root = Path(out)

    update = str(request.config.getoption(LookOption.UPDATE.value))
    return LookSettings(update=update, out=root)


@pytest.fixture(scope="session")
def look_report(
    request: pytest.FixtureRequest, look_settings: LookSettings
) -> Iterator[LookReport]:
    worker = "main"
    if workerinput := getattr(request.config, "workerinput", None):
        worker = str(workerinput["workerid"])

    report = LookReport(look_settings.out, worker)
    try:
        yield report
    finally:
        report.write()


@pytest.fixture(scope="session", autouse=True)
def look_ready(browser: Browser, look_settings: LookSettings) -> None:
    """Сборка фронта и окружение снимков те же, что у эталонов: иначе любая
    разница — ложная, и набор отказывает одним сообщением."""
    FrontBuild().check()
    environments = LookEnvironments()
    environments.check(environments.current(browser), look_settings)


@pytest.fixture
def look(
    request: pytest.FixtureRequest, look_settings: LookSettings, look_report: LookReport
) -> LookCheck:
    return LookCheck(request.node.name, look_settings, look_report)


@pytest.fixture
def look_pages(browser: Browser) -> Iterator[LookPages]:
    pages = LookPages(browser)
    try:
        yield pages
    finally:
        pages.close()


@pytest.fixture(scope="session")
def look_llm_port() -> Iterator[int]:
    port = free_port()
    process = multiprocessing.Process(
        target=serve,
        args=(StandUrl.HOST.value, port, LookStand.TOKEN_DELAY_SEC),
        daemon=True,
    )
    process.start()
    try:
        health = StandUrl.of(port, FakeRoute.HEALTH.value)
        deadline = time.monotonic() + 30.0
        while True:
            try:
                httpx.get(health, timeout=1.0)
                break
            except httpx.TransportError as exc:
                if time.monotonic() > deadline:
                    msg = f"look fake llm: GET {health} did not answer in 30s: {exc}"
                    raise LookError(msg) from exc

                time.sleep(0.1)

        yield port
    finally:
        process.terminate()
        process.join(timeout=10)


@pytest.fixture(scope="session")
def look_stands(
    tmp_path_factory: pytest.TempPathFactory, look_llm_port: int
) -> Iterator[LookStand]:
    database = StandDatabase(StandApp.AID, StandNames().of(LookStand.DATABASE))
    stands = LookStand(
        tmp_path_factory.mktemp("look-stand"), look_llm_port, database.prepare()
    )
    try:
        yield stands
    finally:
        stands.stop()
