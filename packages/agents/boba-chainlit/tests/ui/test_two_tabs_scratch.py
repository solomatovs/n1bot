"""Разведка: вторая вкладка того же треда в одном процессе — видит ли она ход."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, WebSocket

from ui.chat_page import ChatPage
from ui.fake_llm import ScenarioName, serve
from ui.socket_log import ChatEvent, SocketLog
from ui.stand import StandConfig, StandProcess, StandUrl, free_port

pytestmark = pytest.mark.ui

SLOW_TOKEN_SEC = 1.0
BOOT_TIMEOUT_SEC = 180.0


@pytest.fixture(scope="module")
def slow_llm_port() -> Iterator[int]:
    port = free_port()
    process = multiprocessing.Process(
        target=serve, args=("127.0.0.1", port, SLOW_TOKEN_SEC), daemon=True
    )
    process.start()
    for _ in range(100):
        try:
            if httpx.get(StandUrl.of(port, "/health"), timeout=1.0).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    try:
        yield port
    finally:
        process.terminate()
        process.join(timeout=10)


@pytest.fixture(scope="module")
def slow_stand(
    stand_workdir: Path, slow_llm_port: int, stand_database: str
) -> Iterator[StandProcess]:
    config = StandConfig(
        workdir=stand_workdir / "two-tabs",
        app_port=free_port(),
        llm_port=slow_llm_port,
        db_name=stand_database,
        url_prefix="/boba-tabs",
    )
    process = StandProcess(config=config, log_path=stand_workdir / "tabs-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


def _thread_id(log: SocketLog, timeout_sec: float, page: Page) -> str:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        for frame in log.of_event(ChatEvent.NEW_MESSAGE):
            if isinstance(frame.payload, dict) and frame.payload.get("threadId"):
                return str(frame.payload["threadId"])
        page.wait_for_timeout(100)
    raise AssertionError(f"no threadId\n{log.describe()}")


def _second_tab(first: ChatPage, url: str) -> tuple[Page, SocketLog]:
    page = first.page.context.new_page()
    log = SocketLog()

    def on_socket(socket: WebSocket) -> None:
        socket.on("framereceived", log.accept)

    page.on("websocket", on_socket)
    page.goto(url, wait_until="domcontentloaded")
    return page, log


def _report(name: str, page: Page, log: SocketLog, elapsed: float) -> dict[str, Any]:
    events: dict[str, int] = {}
    for frame in log.frames:
        events[frame.event.value] = events.get(frame.event.value, 0) + 1
    return {
        "tab": name,
        "t": round(elapsed, 1),
        "stop_button": page.locator("#stop-button").count(),
        "url": page.url,
        "events": events,
    }


def test_second_tab_opened_during_turn(slow_stand: StandProcess, open_chat: Any) -> None:
    chat1 = open_chat(slow_stand)
    chat1.ask(f"{ScenarioName.THINKING_ANSWER.value} please")
    thread_id = _thread_id(chat1.log, 30.0, chat1.page)
    started = time.monotonic()

    page2, log2 = _second_tab(chat1, f"{slow_stand.config.base_url}/thread/{thread_id}")
    page2.wait_for_timeout(4000)
    snapshots = [_report("tab2", page2, log2, time.monotonic() - started)]
    snapshots.append(_report("tab1", chat1.page, chat1.log, time.monotonic() - started))

    chat1.await_idle(timeout_sec=90.0)
    page2.wait_for_timeout(1500)
    snapshots.append(_report("tab2-end", page2, log2, time.monotonic() - started))

    print("\nTHREAD", thread_id)
    for snapshot in snapshots:
        print("SNAPSHOT", snapshot)
    print("TAB2 FRAMES\n" + log2.describe())
    tail = slow_stand.tail(60)
    print("APP LOG TAIL\n" + tail)


def test_second_tab_opened_before_turn(slow_stand: StandProcess, open_chat: Any) -> None:
    chat1 = open_chat(slow_stand)
    chat1.ask(f"{ScenarioName.ANSWER.value} warmup")
    thread_id = _thread_id(chat1.log, 30.0, chat1.page)
    chat1.await_idle(timeout_sec=90.0)

    page2, log2 = _second_tab(chat1, f"{slow_stand.config.base_url}/thread/{thread_id}")
    page2.wait_for_timeout(3000)
    log2.clear()

    started = time.monotonic()
    chat1.ask(f"{ScenarioName.THINKING_ANSWER.value} second")
    page2.wait_for_timeout(5000)
    snapshots = [_report("tab2", page2, log2, time.monotonic() - started)]
    snapshots.append(_report("tab1", chat1.page, chat1.log, time.monotonic() - started))

    chat1.await_idle(timeout_sec=90.0)
    page2.wait_for_timeout(1500)
    snapshots.append(_report("tab2-end", page2, log2, time.monotonic() - started))

    print("\nTHREAD", thread_id)
    for snapshot in snapshots:
        print("SNAPSHOT", snapshot)
    print("TAB2 FRAMES\n" + log2.describe())
    print("APP LOG TAIL\n" + slow_stand.tail(60))
