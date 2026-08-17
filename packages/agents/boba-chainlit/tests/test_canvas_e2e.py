"""Канвас в живом браузере: что реально попало в DOM панели по типам файлов.

Поднимает приложение, логинится локальным пользователем, кладёт файлы в
workspace открытого треда и просит панель показать каждый — затем проверяет
DOM: картинка загрузилась, диаграмма отрисована в svg, текст виден,
неподдерживаемый формат объяснён.

Запуск: BOBA_CONFIG_PATH=... pytest -m integration
packages/agents/boba-chainlit/tests/test_canvas_e2e.py
Нужны: playwright + chromium, postgres, образ workspace и делегированный
cgroup base (иначе песочница не стартует — boba-cgroup.service).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeUrl

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPO = Path(__file__).resolve().parents[4]
LAUNCHER = REPO / ".venv/bin/python"
ENTRY = REPO / "packages/agents/boba-chainlit/src/boba/chainlit/main.py"
PORT = int(os.environ.get("BOBA_E2E_PORT", "8601"))
BASE = FakeUrl.loopback(PORT, "/boba-debug")
USER_ID = "1"
LOGIN = ("admin", "myPassdfd3")

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000a0000000a0806000000"
    "8d32cfbd0000001a49444154789c63fcffff3f0324a6018a03a0d80751"
    "8c00e30600002e2c0201f3ba9d5c0000000049454e44ae426082"
)
SVG = (Path(__file__).parent / "fixtures" / "probe.svg").read_bytes()
BROKEN_MMD = (
    "flowchart LR\n"
    '    A["Доход"] --> B["Вычеты"]\n'
    "\n"
    '    subgraph VYCHE[ "Структура вычетов" ]\n'
    '        B1["Стандартный"]\n'
    "    end\n"
    "    B1 --> B\n"
)
"""Пробелы внутри скобок подграфа — mermaid такое не принимает (реальный случай)."""

FILES: dict[str, bytes] = {
    "chart.png": PNG,
    "chart.svg": SVG,
    "flow.mmd": "flowchart LR\n    A[Доход] --> B[Налог]\n".encode(),
    "report.md": "# Отчёт\n\n- доход: 4 786 066\n".encode(),
    "data.bin": b"\x00\x01\x02\x03unsupported",
    "broken.mmd": BROKEN_MMD.encode(),
}


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def app_server() -> Iterator[None]:
    if not os.environ.get("BOBA_CONFIG_PATH"):
        pytest.skip("BOBA_CONFIG_PATH не задан")

    # чужой процесс на порту молча увёл бы тесты на другой код
    probe = socket.socket()
    try:
        taken = probe.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        probe.close()

    if taken:
        pytest.fail(f"порт {PORT} уже занят: остановите запущенное приложение")

    log = Path(tempfile.gettempdir()) / "boba-canvas-e2e.log"
    process = subprocess.Popen(  # noqa: S603
        [str(LAUNCHER), str(ENTRY)],
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        yield
    finally:
        process.terminate()
        process.wait(timeout=15)


async def _wait_for_server() -> None:
    last_error = "нет ответа"

    async with httpx.AsyncClient() as probe:
        for _ in range(90):
            try:
                answer = await probe.get(BASE + "/login", follow_redirects=True)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                await asyncio.sleep(1)
                continue

            if answer.status_code < 500:
                return

            last_error = f"HTTP {answer.status_code}"
            await asyncio.sleep(1)

    pytest.fail(f"приложение не поднялось: {last_error}")


async def _upload(thread_id: str) -> None:
    from boba.chainlit.data.storage import StorageFactory
    from boba.chainlit.infra.config import AppConfig
    from boba.settings import bind, build_app_config

    raw = build_app_config(config_path=Path(os.environ["BOBA_CONFIG_PATH"]))
    config = bind(raw, path="app", model=AppConfig)
    storage = StorageFactory.create(config.storage)

    for name, blob in FILES.items():
        await asyncio.wait_for(
            storage.upload_file(f"{USER_ID}/{thread_id}/upload/{name}", blob), 120
        )


class _SessionProbe:
    """Слушатели страницы: sessionId и threadId из трафика, отчёты канваса."""

    def __init__(self) -> None:
        self.session: dict[str, str] = {}
        self.reports: list[str] = []

    def watch(self, page: Any) -> None:
        page.on("request", self._on_request)
        page.on("websocket", self._on_websocket)

    def _on_request(self, request: Any) -> None:
        if request.method != "POST":
            return

        body = request.post_data
        if not body:
            return

        if request.url.endswith("/project/action"):
            if '"canvas_render_status"' in body:
                self.reports.append(body)
            return

        if "socket.io" not in request.url:
            return

        found = re.search(r'"sessionId"\s*:\s*"([^"]+)"', body)
        if found:
            self.session["id"] = found.group(1)

    def _on_websocket(self, ws: Any) -> None:
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, payload: Any) -> None:
        if isinstance(payload, bytes):
            return

        found = re.search(r'"threadId"\s*:\s*"([0-9a-f-]{36})"', payload)
        if found:
            self.session.setdefault("thread", found.group(1))


@pytest.fixture(scope="module")
async def panel(app_server: None) -> AsyncIterator[Any]:
    """Логин, живой тред и файлы в нём; отдаёт функцию показа файла в панели."""
    playwright = pytest.importorskip("playwright.async_api")

    await _wait_for_server()

    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await context.new_page()

        probe = _SessionProbe()
        session = probe.session
        reports = probe.reports
        probe.watch(page)

        await page.goto(BASE + "/login")
        await page.wait_for_selector("input")
        inputs = page.locator("form input")
        await inputs.nth(0).fill(LOGIN[0])
        await inputs.nth(1).fill(LOGIN[1])
        await page.locator('form button[type="submit"]').first.click()

        for _ in range(30):
            if "id" in session and "thread" in session:
                break
            await page.wait_for_timeout(500)

        if "id" not in session:
            raise AssertionError("не удалось получить sessionId")
        if "thread" not in session:
            raise AssertionError("не удалось получить threadId")

        await _upload(session["thread"])

        async def act(name: str, action_payload: dict[str, str]) -> Any:
            payload = {
                "sessionId": session["id"],
                "action": {
                    "name": name,
                    "payload": action_payload,
                    "label": "",
                    "tooltip": "",
                    "icon": None,
                    "forId": None,
                    "id": "e2e",
                },
            }
            await page.request.post(
                BASE + "/project/action",
                data=json.dumps(payload),
                headers={"content-type": "application/json"},
            )
            await page.wait_for_timeout(3000)
            return page.locator("#side-view-content")

        async def show(name: str) -> Any:
            path = f"/workspace/{session['thread']}/upload/{name}"
            return await act("canvas_open", {"path": path})

        yield show, reports, session["thread"], act

        await browser.close()


async def test_image_is_rendered(panel: Any) -> None:
    """Картинка от bash/python-тула должна реально загрузиться, а не быть битой."""
    show, _, _thread, _act = panel
    side = await show("chart.png")
    image = side.locator("img").first

    if not (await image.count()):
        raise AssertionError("await image.count()")
    # ждём саму загрузку: на холодном старте она приходит позже показа
    await side.page.wait_for_function(
        """() => {
            const img = document.querySelector('#side-view-content img');
            return !!img && img.complete && img.naturalWidth > 0;
        }""",
        timeout=15000,
    )


async def test_svg_is_rendered(panel: Any) -> None:
    show, _, _thread, _act = panel
    side = await show("chart.svg")

    await side.page.wait_for_function(
        """() => {
            const img = document.querySelector('#side-view-content img');
            return !!img && img.complete && img.naturalWidth > 0;
        }""",
        timeout=15000,
    )


async def test_diagram_is_drawn_not_shown_as_text(panel: Any) -> None:
    """Спека mermaid превращается в svg, а не показывается текстом файла."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    text = await side.inner_text()

    if await side.locator('div[style*="transform-origin"] svg').count() <= 0:
        raise AssertionError(
            "await side.locator('div[style*=\"transform-origin\"] svg'…"
        )
    if "Доход" not in text:
        raise AssertionError('"Доход" in text')
    # имя файла в панели не дублируем: шапка отнимала бы место у диаграммы
    if "flow.mmd" in text:
        raise AssertionError('"flow.mmd" not in text')


async def test_close_button_does_not_cover_the_diagram(panel: Any) -> None:
    """В canvas-режиме chainlit красит кнопку закрытия в primary; css её гасит."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    close = side.page.locator("#side-view-title button").first

    style = await close.evaluate(
        "el => { const s = getComputedStyle(el);"
        " return {bg: s.backgroundColor, opacity: s.opacity}; }"
    )

    if style["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"):
        raise AssertionError('style["bg"] in ("rgba(0, 0, 0, 0)", "transparent")')
    if float(style["opacity"]) >= 1:
        raise AssertionError('float(style["opacity"]) < 1')


async def test_markdown_is_rendered(panel: Any) -> None:
    show, _, _thread, _act = panel
    side = await show("report.md")

    if "Отчёт" not in await side.inner_text():
        raise AssertionError('"Отчёт" in await side.inner_text()')


async def test_unsupported_format_is_explained(panel: Any) -> None:
    """Неподдерживаемый формат: панель объясняет, а не молчит и не врёт.

    Имени файла в панели нет намеренно: шапки у канваса убраны, объяснение
    живёт по центру пустой панели.
    """
    show, _, _thread, _act = panel
    side = await show("data.bin")
    text = await side.inner_text()

    if "cannot display" not in text:
        raise AssertionError('"cannot display" in text')


async def test_broken_spec_verdict_reaches_server(panel: Any) -> None:
    """Ошибку синтаксиса видит только браузер: плашка в панели, вердикт — серверу."""
    show, reports, _thread, _act = panel
    side = await show("broken.mmd")
    text = await side.inner_text()

    if "not rendered" not in text:
        raise AssertionError('"not rendered" in text')
    if "Parse error" not in text:
        raise AssertionError('"Parse error" in text')

    failures = [
        body for body in reports if '"ok": false' in body or '"ok":false' in body
    ]
    if not (failures):
        raise AssertionError("браузер не отправил canvas_render_status с ошибкой")
    if "Parse error" not in failures[-1]:
        raise AssertionError('"Parse error" in failures[-1]')


async def test_diagram_fills_the_panel(panel: Any) -> None:
    """Диаграмма занимает всю панель: mermaid отдаёт svg в своих пикселях."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")

    box = await side.bounding_box()
    svg = await side.locator('div[style*="transform-origin"] svg').first.bounding_box()

    if not (box):
        raise AssertionError("box")
    if not (svg):
        raise AssertionError("svg")
    if svg["width"] <= box["width"] * 0.9:
        raise AssertionError('svg["width"] > box["width"] * 0.9')
    if svg["height"] <= box["height"] * 0.8:
        raise AssertionError('svg["height"] > box["height"] * 0.8')


async def test_wheel_does_not_zoom_in_the_panel(panel: Any) -> None:
    """Колесо в панели листает страницу; зум остаётся полноэкранному режиму."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    read = (
        "() => document.querySelector("
        "'#side-view-content div[style*=\"transform-origin\"] svg')"
        ".parentElement.style.transform"
    )
    page = side.page

    before = await page.evaluate(read)
    # наводим в центр: сверху висит кнопка закрытия панели
    box = await side.bounding_box()
    if not (box):
        raise AssertionError("box")
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await page.mouse.wheel(0, -400)
    await page.wait_for_timeout(400)

    if await page.evaluate(read) != before:
        raise AssertionError("await page.evaluate(read) == before")


async def test_switching_file_does_not_reopen_the_panel(panel: Any) -> None:
    """Открытая панель меняет содержимое сама: анимация открытия не повторяется."""
    show, _, thread, _act = panel
    other = f"/workspace/{thread}/upload/report.md"
    side = await show("flow.mmd")
    page = side.page

    await page.evaluate(
        """() => {
            const content = document.querySelector('#side-view-content');
            const node = content.closest('[class*="translate-x-"]');
            window.__reopens = 0;
            new MutationObserver(() => { window.__reopens += 1; })
              .observe(node, {attributes: true, attributeFilter: ['class']});
            window.__panelNode = node;
        }"""
    )

    await page.evaluate(
        """path => window.dispatchEvent(
            new CustomEvent('boba:canvas', {detail: {path}}))""",
        other,
    )
    await page.wait_for_timeout(2500)

    if "Отчёт" not in await side.inner_text():
        raise AssertionError('"Отчёт" in await side.inner_text()')

    same_node = await page.evaluate(
        "() => window.__panelNode === document.querySelector('#side-view-content')"
        ".closest('[class*=translate-x-]')"
    )

    if not (same_node):
        raise AssertionError(
            "панель пересоздалась — анимация открытия проиграется заново"
        )
    if await page.evaluate("() => window.__reopens") != 0:
        raise AssertionError('await page.evaluate("() => window.__reopens") == 0')


async def test_controls_share_one_alignment(panel: Any) -> None:
    """Кнопки шапки — один ряд по стандарту chainlit: 36px, закрытие справа.

    Родной «назад» панели спрятан: закрытие живёт последней кнопкой ряда,
    как в полноэкранном режиме.
    """
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    geometry = await page.evaluate(
        """() => {
            const panel = document.querySelector('#side-view-content')
              .closest('.bg-card').getBoundingClientRect();
            const back = document.querySelector('#side-view-title');
            const buttons = [...document.querySelectorAll(
              '#side-view-content button[aria-label]'
            )].map(b => {
              const r = b.getBoundingClientRect();
              return {
                label: b.getAttribute('aria-label'),
                top: Math.round(r.top - panel.top),
                right: Math.round(panel.right - r.right),
                size: Math.round(r.width),
              };
            });
            return {
              backHidden: !back || getComputedStyle(back).display === 'none',
              buttons,
            };
        }"""
    )

    if geometry["backHidden"] is not True:
        raise AssertionError('geometry["backHidden"] is True')
    if not (geometry["buttons"]):
        raise AssertionError('geometry["buttons"]')
    tops = {entry["top"] for entry in geometry["buttons"]}
    if max(tops) - min(tops) > 2:
        raise AssertionError("max(tops) - min(tops) <= 2")
    for entry in geometry["buttons"]:
        if entry["size"] != 36:
            raise AssertionError('entry["size"] == 36')

    last = geometry["buttons"][-1]
    if last["label"] != "Close":
        raise AssertionError('last["label"] == "Close"')
    if abs(last["right"] - 25) > 4:
        raise AssertionError('abs(last["right"] - 25) <= 4')


async def test_fullscreen_controls_are_on_the_close_line(panel: Any) -> None:
    """В полноэкранном режиме зум стоит на одной линии с кнопкой закрытия."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    await side.locator('button[aria-label="Fullscreen"]').click()
    await page.wait_for_timeout(1000)

    rows = await page.evaluate(
        """() => {
            const dialog = document.querySelector('[role="dialog"]');
            const box = dialog.getBoundingClientRect();
            return [...dialog.querySelectorAll('button')]
              .filter(b => getComputedStyle(b).display !== 'none')
              .map(b => {
                const r = b.getBoundingClientRect();
                return Math.round(r.top - box.top);
              });
        }"""
    )

    if not (rows):
        raise AssertionError("rows")
    if max(rows) - min(rows) > 2:
        raise AssertionError("max(rows) - min(rows) <= 2")


async def test_broken_diagram_keeps_the_control_row(panel: Any) -> None:
    """Панель со сломанной диаграммой — та же панель: ряд кнопок обязан быть.

    Раньше аварийные ветки вьюверов рисовались мимо рамки, и пользователь
    оставался без зума, полноэкранного режима и закрытия.
    """
    show, _, _thread, _act = panel
    side = await show("broken.mmd")

    labels = await side.page.evaluate(
        """() => [...document.querySelectorAll(
            '#side-view-content button[aria-label]'
        )].map(b => b.getAttribute('aria-label'))"""
    )

    if labels[-2:] != ["Fullscreen", "Close"]:
        raise AssertionError('labels[-2:] == ["Fullscreen", "Close"]')
    if "Zoom in" not in labels:
        raise AssertionError('"Zoom in" in labels')
    if "Zoom out" not in labels:
        raise AssertionError('"Zoom out" in labels')
    if "Reset view" not in labels:
        raise AssertionError('"Reset view" in labels')


async def test_unsupported_format_keeps_the_control_row(panel: Any) -> None:
    """Объяснение «формат не показать» тоже живёт в общей рамке."""
    show, _, _thread, _act = panel
    side = await show("data.bin")

    labels = await side.page.evaluate(
        """() => [...document.querySelectorAll(
            '#side-view-content button[aria-label]'
        )].map(b => b.getAttribute('aria-label'))"""
    )

    if labels[-2:] != ["Fullscreen", "Close"]:
        raise AssertionError('labels[-2:] == ["Fullscreen", "Close"]')


async def test_file_link_does_not_depend_on_the_session(panel: Any) -> None:
    """Ссылка панели адресует файл, а не запись в памяти сессии.

    Сессионная ссылка умирала вместе с сокетом (перезапуск приложения,
    переподключение вкладки), и картинка молча превращалась в битый img.
    """
    show, _, thread, _act = panel
    side = await show("chart.svg")

    src = await side.page.evaluate(
        "() => document.querySelector('#side-view-content img').src"
    )

    if "session_id" in src:
        raise AssertionError('"session_id" not in src')
    if f"/canvas/{thread}/upload/chart.svg" not in src:
        raise AssertionError('f"/canvas/{thread}/upload/chart.svg" in src')


async def test_panel_switches_after_a_render_error(panel: Any) -> None:
    """Сломанная диаграмма не должна запирать панель: следующий файл рисуется."""
    show, _, thread, _act = panel
    side = await show("broken.mmd")
    page = side.page

    if "not rendered" not in await side.inner_text():
        raise AssertionError('"not rendered" in await side.inner_text()')

    await page.evaluate(
        """path => window.dispatchEvent(
            new CustomEvent('boba:canvas', {detail: {path}}))""",
        f"/workspace/{thread}/upload/flow.mmd",
    )
    await page.wait_for_timeout(3000)

    text = await side.inner_text()
    if "Доход" not in text:
        raise AssertionError('"Доход" in text')
    if "not rendered" in text:
        raise AssertionError('"not rendered" not in text')


async def test_stream_channel_delivers_to_the_panel(panel: Any) -> None:
    """Канал живого вывода доезжает до DOM панели.

    Ход агента в e2e не гоняется, поэтому потока нет — но объяснение
    «поток недоступен» идёт в панель тем же каналом, каким насос шлёт
    снапшоты окна: доставка action -> канал -> сокет -> DOM проверяется
    без инструмента.
    """
    _show, _, _thread, act = panel
    side = await act("canvas_stream", {"call_id": "e2e-нет-такого-вызова"})

    if "unavailable" not in await side.inner_text():
        raise AssertionError('"unavailable" in await side.inner_text()')


async def test_panel_close_button_closes_the_panel(panel: Any) -> None:
    """Закрытие в ряду кнопок реально закрывает панель канваса."""
    show, _, _thread, _act = panel
    side = await show("report.md")
    page = side.page

    await side.locator('button[aria-label="Close"]').click()
    await page.wait_for_timeout(1500)

    if await page.locator("#side-view-content").count() != 0:
        raise AssertionError('await page.locator("#side-view-content").count() == 0')


async def test_bar_survives_scrolling(panel: Any) -> None:
    """Прокрутка содержимого не уводит шапку: кнопки доступны всегда."""
    show, _, _thread, _act = panel
    side = await show("report.md")
    page = side.page

    before = await page.evaluate(
        """() => document.querySelector(
            '#side-view-content button[aria-label]'
        ).getBoundingClientRect().top"""
    )

    await page.evaluate(
        """() => {
            for (const el of document.querySelectorAll(
              '#side-view-content div'
            )) {
              if (el.scrollHeight > el.clientHeight) el.scrollTop = 99999;
            }
        }"""
    )
    await page.wait_for_timeout(300)

    after = await page.evaluate(
        """() => {
            const button = document.querySelector(
              '#side-view-content button[aria-label]'
            );
            const r = button.getBoundingClientRect();
            return { top: r.top, height: r.height };
        }"""
    )

    if after["height"] <= 0:
        raise AssertionError('after["height"] > 0')
    if abs(after["top"] - before) > 2:
        raise AssertionError('abs(after["top"] - before) <= 2')
