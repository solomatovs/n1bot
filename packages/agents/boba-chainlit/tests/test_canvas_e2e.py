"""Канвас в живом браузере: что реально попало в DOM панели по типам файлов.

Поднимает приложение, логинится локальным пользователем, кладёт файлы в
workspace открытого треда и просит панель показать каждый — затем проверяет
DOM: картинка загрузилась, диаграмма отрисована в svg, текст виден,
неподдерживаемый формат объяснён.

Запуск: BOBA_CONFIG_PATH=... pytest -m integration packages/agents/boba-chainlit/tests/test_canvas_e2e.py
Нужны: playwright + chromium, postgres, образ workspace и делегированный
systemd-scope (иначе песочница не стартует — см. .vscode/python-delegated.sh).
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

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPO = Path(__file__).resolve().parents[4]
LAUNCHER = REPO / ".vscode/python-delegated.sh"
ENTRY = REPO / "packages/agents/boba-chainlit/src/boba/chainlit/main.py"
PORT = int(os.environ.get("BOBA_E2E_PORT", "8601"))
BASE = f"http://127.0.0.1:{PORT}/boba-debug"
USER_ID = "1"
LOGIN = ("admin", "myPassdfd3")

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000a0000000a0806000000"
    "8d32cfbd0000001a49444154789c63fcffff3f0324a6018a03a0d80751"
    "8c00e30600002e2c0201f3ba9d5c0000000049454e44ae426082"
)
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">'
    '<rect width="120" height="40" fill="#4c7cf0"/></svg>'
).encode()
BROKEN_MMD = (
    'flowchart LR\n'
    '    A["Доход"] --> B["Вычеты"]\n'
    '\n'
    '    subgraph VYCHE[ "Структура вычетов" ]\n'
    '        B1["Стандартный"]\n'
    '    end\n'
    '    B1 --> B\n'
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
        probe.connect(("127.0.0.1", PORT))
    except OSError:
        pass
    else:
        pytest.fail(f"порт {PORT} уже занят: остановите запущенное приложение")
    finally:
        probe.close()

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
    async with httpx.AsyncClient() as probe:
        for _ in range(90):
            try:
                answer = await probe.get(BASE + "/login", follow_redirects=True)
                if answer.status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)

    pytest.fail("приложение не поднялось")


async def _upload(thread_id: str) -> None:
    from boba.chainlit.chat.data.storage import StorageFactory
    from boba.chainlit.infra.config import AppConfig
    from boba.settings import bind, build_app_config

    raw = build_app_config(config_path=Path(os.environ["BOBA_CONFIG_PATH"]))
    config = bind(raw, path="app", model=AppConfig)
    storage = StorageFactory.create(config.storage)

    for name, blob in FILES.items():
        await asyncio.wait_for(
            storage.upload_file(f"{USER_ID}/{thread_id}/upload/{name}", blob), 120
        )


@pytest.fixture(scope="module")
async def panel(app_server: None) -> AsyncIterator[Any]:
    """Логин, живой тред и файлы в нём; отдаёт функцию показа файла в панели."""
    playwright = pytest.importorskip("playwright.async_api")

    await _wait_for_server()

    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await context.new_page()

        session: dict[str, str] = {}
        reports: list[str] = []

        def on_request(request: Any) -> None:
            if request.method != "POST":
                return
            body = request.post_data
            if not body:
                return

            if request.url.endswith("/project/action"):
                if '"canvas_render_status"' in body:
                    reports.append(body)
                return

            if "socket.io" not in request.url:
                return

            found = re.search(r'"sessionId"\s*:\s*"([^"]+)"', body)
            if found:
                session["id"] = found.group(1)

        def on_websocket(ws: Any) -> None:
            def on_frame(payload: Any) -> None:
                if isinstance(payload, bytes):
                    return
                found = re.search(r'"threadId"\s*:\s*"([0-9a-f-]{36})"', payload)
                if found:
                    session.setdefault("thread", found.group(1))

            ws.on("framereceived", on_frame)

        page.on("request", on_request)
        page.on("websocket", on_websocket)

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

        assert "id" in session, "не удалось получить sessionId"
        assert "thread" in session, "не удалось получить threadId"

        await _upload(session["thread"])

        async def show(name: str) -> Any:
            payload = {
                "sessionId": session["id"],
                "action": {
                    "name": "canvas_open",
                    "payload": {
                        "path": f"/workspace/{session['thread']}/upload/{name}"
                    },
                    "label": "", "tooltip": "", "icon": None,
                    "forId": None, "id": "e2e",
                },
            }
            await page.request.post(
                BASE + "/project/action",
                data=json.dumps(payload),
                headers={"content-type": "application/json"},
            )
            await page.wait_for_timeout(3000)
            return page.locator("#side-view-content")

        yield show, reports, session["thread"]

        await browser.close()


async def test_image_is_rendered(panel: Any) -> None:
    """Картинка от bash/python-тула должна реально загрузиться, а не быть битой."""
    show, _, _thread = panel
    side = await show("chart.png")
    image = side.locator("img").first

    assert await image.count()
    # ждём саму загрузку: на холодном старте она приходит позже показа
    await side.page.wait_for_function(
        """() => {
            const img = document.querySelector('#side-view-content img');
            return !!img && img.complete && img.naturalWidth > 0;
        }""",
        timeout=15000,
    )


async def test_svg_is_rendered(panel: Any) -> None:
    show, _, _thread = panel
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
    show, _, _thread = panel
    side = await show("flow.mmd")
    text = await side.inner_text()

    assert await side.locator("svg").count() > 0
    assert "Доход" in text
    # имя файла в панели не дублируем: шапка отнимала бы место у диаграммы
    assert "flow.mmd" not in text


async def test_close_button_does_not_cover_the_diagram(panel: Any) -> None:
    """В canvas-режиме chainlit красит кнопку закрытия в primary; css её гасит."""
    show, _, _thread = panel
    side = await show("flow.mmd")
    close = side.page.locator("#side-view-title button").first

    style = await close.evaluate(
        "el => { const s = getComputedStyle(el);"
        " return {bg: s.backgroundColor, opacity: s.opacity}; }"
    )

    assert style["bg"] in ("rgba(0, 0, 0, 0)", "transparent")
    assert float(style["opacity"]) < 1


async def test_markdown_is_rendered(panel: Any) -> None:
    show, _, _thread = panel
    side = await show("report.md")

    assert "Отчёт" in await side.inner_text()


async def test_unsupported_format_is_explained(panel: Any) -> None:
    """Неподдерживаемый формат: панель объясняет, а не молчит и не врёт."""
    show, _, _thread = panel
    side = await show("data.bin")
    text = await side.inner_text()

    assert "data.bin" in text
    assert "показать не умеет" in text


async def test_broken_spec_verdict_reaches_server(panel: Any) -> None:
    """Ошибку синтаксиса видит только браузер: плашка в панели, вердикт — серверу."""
    show, reports, _thread = panel
    side = await show("broken.mmd")
    text = await side.inner_text()

    assert "не отрисована" in text
    assert "Parse error" in text

    failures = [body for body in reports if '"ok": false' in body or '"ok":false' in body]
    assert failures, "браузер не отправил canvas_render_status с ошибкой"
    assert "Parse error" in failures[-1]


async def test_diagram_fills_the_panel(panel: Any) -> None:
    """Диаграмма занимает всю панель: mermaid отдаёт svg в своих пикселях."""
    show, _, _thread = panel
    side = await show("flow.mmd")

    box = await side.bounding_box()
    svg = await side.locator("svg").first.bounding_box()

    assert box and svg
    assert svg["width"] > box["width"] * 0.9
    assert svg["height"] > box["height"] * 0.8


async def test_wheel_does_not_zoom_in_the_panel(panel: Any) -> None:
    """Колесо в панели листает страницу; зум остаётся полноэкранному режиму."""
    show, _, _thread = panel
    side = await show("flow.mmd")
    read = (
        "() => document.querySelector('#side-view-content svg')"
        ".parentElement.style.transform"
    )
    page = side.page

    before = await page.evaluate(read)
    # наводим в центр: сверху висит кнопка закрытия панели
    box = await side.bounding_box()
    assert box
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await page.mouse.wheel(0, -400)
    await page.wait_for_timeout(400)

    assert await page.evaluate(read) == before


async def test_switching_file_does_not_reopen_the_panel(panel: Any) -> None:
    """Открытая панель меняет содержимое сама: анимация открытия не повторяется."""
    show, _, thread = panel
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

    assert "Отчёт" in await side.inner_text()

    same_node = await page.evaluate(
        "() => window.__panelNode === document.querySelector('#side-view-content')"
        ".closest('[class*=translate-x-]')"
    )

    assert same_node, "панель пересоздалась — анимация открытия проиграется заново"
    assert await page.evaluate("() => window.__reopens") == 0


async def test_controls_share_one_alignment(panel: Any) -> None:
    """Кнопки канваса стоят по стандарту chainlit: 16px от краёв, размер icon."""
    show, _, _thread = panel
    side = await show("flow.mmd")
    page = side.page

    geometry = await page.evaluate(
        """() => {
            const panel = document.querySelector('#side-view-content')
              .closest('[class*="translate-x-"]').getBoundingClientRect();
            const close = document.querySelector('#side-view-title button')
              .getBoundingClientRect();
            const zoom = document.querySelector('#side-view-content button')
              .getBoundingClientRect();
            return {
              closeTop: Math.round(close.top - panel.top),
              closeLeft: Math.round(close.left - panel.left),
              zoomTop: Math.round(zoom.top - panel.top),
              size: Math.round(close.width),
            };
        }"""
    )

    assert geometry["size"] == 36
    assert abs(geometry["closeTop"] - 16) <= 2
    assert abs(geometry["closeLeft"] - 16) <= 2
    assert abs(geometry["zoomTop"] - geometry["closeTop"]) <= 2


async def test_fullscreen_controls_are_on_the_close_line(panel: Any) -> None:
    """В полноэкранном режиме зум стоит на одной линии с кнопкой закрытия."""
    show, _, _thread = panel
    side = await show("flow.mmd")
    page = side.page

    await side.locator('button[aria-label="Во весь экран"]').click()
    await page.wait_for_timeout(1000)

    rows = await page.evaluate(
        """() => {
            const dialog = document.querySelector('[role="dialog"]');
            const box = dialog.getBoundingClientRect();
            return [...dialog.querySelectorAll('button')].map(b => {
              const r = b.getBoundingClientRect();
              return Math.round(r.top - box.top);
            });
        }"""
    )

    assert rows
    assert max(rows) - min(rows) <= 2


async def test_panel_switches_after_a_render_error(panel: Any) -> None:
    """Сломанная диаграмма не должна запирать панель: следующий файл рисуется."""
    show, _, thread = panel
    side = await show("broken.mmd")
    page = side.page

    assert "не отрисована" in await side.inner_text()

    await page.evaluate(
        """path => window.dispatchEvent(
            new CustomEvent('boba:canvas', {detail: {path}}))""",
        f"/workspace/{thread}/upload/flow.mmd",
    )
    await page.wait_for_timeout(3000)

    text = await side.inner_text()
    assert "Доход" in text
    assert "не отрисована" not in text
