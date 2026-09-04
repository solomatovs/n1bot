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
from chainlit_stand import FakeUrl

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPO = Path(__file__).resolve().parents[4]
LAUNCHER = REPO / ".venv/bin/python"
ENTRY = REPO / "packages/agents/boba-chainlit/src/boba/chainlit/main.py"
PORT = int(os.environ.get("BOBA_E2E_PORT", "8601"))
BASE = FakeUrl.loopback(PORT, "/boba-debug")
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
    "notes.log": ("строка журнала\n" * 40).encode(),
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
    process = subprocess.Popen(
        # тест запускает собственный лончер конфигом стенда
        # nosemgrep: dangerous-subprocess-use-tainted-env-args
        [str(LAUNCHER), str(ENTRY), "--config", os.environ["BOBA_CONFIG_PATH"]],
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


def _tool_view(thread_id: str, name: str) -> str:
    """Путь файла глазами тела инструмента: гостевой в песочнице, хостовый в process."""
    from omegaconf import OmegaConf

    from boba.runtime.config import AppLayers

    raw = AppLayers.compose(Path(os.environ["BOBA_CONFIG_PATH"]))
    launcher = str(OmegaConf.select(raw, "env.tool_launcher"))

    if launcher == "process":
        workdir = str(OmegaConf.select(raw, "tool_launcher.workdir"))
        return f"{workdir}/{thread_id}/upload/{name}"

    return f"/workspace/{thread_id}/upload/{name}"


def _app_config() -> Any:
    from boba.chainlit.infra.config import AppConfig
    from boba.config import bind
    from boba.runtime.config import AppLayers

    raw = AppLayers.compose(Path(os.environ["BOBA_CONFIG_PATH"]))
    return bind(raw, path="app", model=AppConfig)


async def _user_id_of(config: Any, identifier: str) -> str:
    """users.id пользователя стенда по логину: владелец тредов и журналов."""
    from psycopg import sql

    from boba.chat.threads import ChatTable
    from boba.db.postgres import AsyncPostgresPool, SqlNames
    from boba.identity.api import UsersColumn

    pool = AsyncPostgresPool(config.data_layer.postgres)
    await pool.open()
    try:
        query = sql.SQL("select {id} from {users} where {identifier} = %s").format(
            id=SqlNames.ident(UsersColumn.ID),
            users=SqlNames.table(config.data_layer.db_schema, ChatTable.USERS),
            identifier=SqlNames.ident(UsersColumn.IDENTIFIER),
        )
        async with pool.connection() as conn:
            cursor = await conn.execute(query, (identifier,))
            row = await cursor.fetchone()
    finally:
        await pool.close()

    if row is None:
        raise AssertionError(f"stand user {identifier!r} has no users row yet")

    return str(row[0])


async def _upload(thread_id: str) -> None:
    from boba.chainlit.data.storage import StorageFactory

    config = _app_config()
    storage = StorageFactory.create(config.storage)
    owner = await _user_id_of(config, LOGIN[0])

    for name, blob in FILES.items():
        await asyncio.wait_for(
            storage.upload_file(f"{owner}/{thread_id}/upload/{name}", blob), 120
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
            path = _tool_view(session["thread"], name)
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
    other = _tool_view(thread, "report.md")
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
    """В полноэкранном режиме зум стоит на одной линии с кнопкой закрытия.

    Полный экран — CSS-оверлей той же сцены (data-full), а не отдельный
    диалог: DOM не пересоздаётся при разворачивании.
    """
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    await side.locator('button[aria-label="Fullscreen"]').click()
    await page.wait_for_timeout(500)

    rows = await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-panel][data-full="true"]');
            const box = stage.getBoundingClientRect();
            return [...stage.querySelectorAll('button')]
              .filter(b => getComputedStyle(b).display !== 'none')
              .map(b => {
                const r = b.getBoundingClientRect();
                return Math.round(r.top - box.top);
              });
        }"""
    )
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

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
        _tool_view(thread, "flow.mmd"),
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


async def _rewrite(thread_id: str, name: str, blob: bytes) -> None:
    """Переписать файл workspace: так его меняет инструмент между показами."""
    from boba.chainlit.data.storage import StorageFactory

    config = _app_config()
    storage = StorageFactory.create(config.storage)
    owner = await _user_id_of(config, LOGIN[0])

    await asyncio.wait_for(
        storage.upload_file(f"{owner}/{thread_id}/upload/{name}", blob, overwrite=True),
        120,
    )


async def test_diagram_redraws_in_place_when_the_file_changes(panel: Any) -> None:
    """Файл спеки переписан — диаграмма перерисовалась на месте.

    Содержимое не едет пушем: слежение шлёт сигнал, фронт сам забирает
    свежую спеку и рендерит её в той же панели — штамп на DOM переживает
    обновление, панель не переоткрывается.
    """
    show, _, thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    if "Доход" not in await side.inner_text():
        raise AssertionError('"Доход" in await side.inner_text()')

    await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '#side-view-content [data-canvas-stage]');
            stage.dataset.stamp = 'alive';
        }"""
    )

    await _rewrite(
        thread, "flow.mmd", "flowchart LR\n    A[Доход] --> C[Прибыль]\n".encode()
    )

    redrawn = False
    for _ in range(24):
        if "Прибыль" in await side.inner_text():
            redrawn = True
            break
        await page.wait_for_timeout(500)

    if not redrawn:
        raise AssertionError("диаграмма не перерисовалась по сигналу слежения")

    stamp = await page.evaluate(
        """() => document.querySelector(
            '#side-view-content [data-canvas-stage]'
        ).dataset.stamp || ''"""
    )
    if stamp != "alive":
        raise AssertionError("панель пересоздалась при обновлении диаграммы")


async def test_zoom_buttons_scale_the_diagram(panel: Any) -> None:
    """Кнопки зума меняют масштаб сцены; сброс возвращает исходный вид."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    read = (
        "() => document.querySelector("
        "'#side-view-content div[style*=\"transform-origin\"]').style.transform"
    )

    base = await page.evaluate(read)
    await side.locator('button[aria-label="Zoom in"]').click()
    zoomed = await page.evaluate(read)
    await side.locator('button[aria-label="Reset view"]').click()
    reset = await page.evaluate(read)

    if "scale(1)" not in base:
        raise AssertionError('"scale(1)" in base')
    if "scale(1.25)" not in zoomed:
        raise AssertionError('"scale(1.25)" in zoomed')
    if "scale(1)" not in reset:
        raise AssertionError('"scale(1)" in reset')


FULL_GEOMETRY = """() => {
    const stage = document.querySelector('[data-canvas-panel][data-full="true"]');
    if (!stage) return null;
    const box = stage.getBoundingClientRect();
    return {
        width: box.width,
        height: box.height,
        left: box.left,
        top: box.top,
        winWidth: window.innerWidth,
        winHeight: window.innerHeight,
        hosted: stage.parentElement === (
            document.getElementById('root') || document.body),
        transformed: (() => {
            let node = stage.parentElement;
            while (node && node !== document.documentElement) {
                if (getComputedStyle(node).transform !== 'none') return true;
                node = node.parentElement;
            }
            return false;
        })(),
    };
}"""


async def _expand(side: Any) -> Any:
    """Развернуть панель на весь экран и вернуть геометрию сцены."""
    await side.locator('button[aria-label="Fullscreen"]').first.click()
    await side.page.wait_for_timeout(400)
    return await side.page.evaluate(FULL_GEOMETRY)


async def _collapse(page: Any) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)


def _assert_covers_window(geometry: Any, label: str) -> None:
    """Сцена обязана занимать всё окно, а не рамку панели."""
    if geometry is None:
        raise AssertionError(f"{label}: полноэкранная сцена не найдена")
    if geometry["width"] < geometry["winWidth"] * 0.98:
        raise AssertionError(
            f"{label}: ширина {geometry['width']} вместо {geometry['winWidth']}"
        )
    if geometry["height"] < geometry["winHeight"] * 0.98:
        raise AssertionError(
            f"{label}: высота {geometry['height']} вместо {geometry['winHeight']}"
        )
    if abs(geometry["left"]) > 2 or abs(geometry["top"]) > 2:
        raise AssertionError(
            f"{label}: сцена смещена в ({geometry['left']}, {geometry['top']})"
        )


async def test_fullscreen_covers_the_whole_window(panel: Any) -> None:
    """Полный экран занимает окно целиком, а не рамку панели.

    Панель chainlit анимируется трансформацией, а position:fixed внутри
    трансформированного предка меряется от него — поэтому сцена уезжает в
    body. Проверяем реальную геометрию, а не атрибут разворачивания.
    """
    show, _, _thread, _act = panel
    side = await show("flow.mmd")

    geometry = await _expand(side)
    await _collapse(side.page)

    _assert_covers_window(geometry, "диаграмма")
    if geometry["hosted"] is not True:
        raise AssertionError("сцена поднята не в корень приложения")
    if geometry["transformed"] is not False:
        raise AssertionError("у сцены остался трансформированный предок: fixed съедет")


async def test_fullscreen_covers_the_window_for_every_viewer(panel: Any) -> None:
    """Каждый вьювер разворачивается на всё окно: рамка одна на все типы."""
    show, _, _thread, _act = panel

    for name in ("chart.png", "report.md", "data.bin", "notes.log"):
        side = await show(name)
        geometry = await _expand(side)
        await _collapse(side.page)

        _assert_covers_window(geometry, name)


async def test_collapse_returns_the_scene_into_the_panel(panel: Any) -> None:
    """Сворачивание возвращает узел в панель: содержимое снова внутри неё."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    await _expand(side)
    await _collapse(page)

    placement = await page.evaluate(
        """() => {
            const stage = document.querySelector('[data-canvas-panel]');
            const panel = document.querySelector('#side-view-content');
            return {
                full: stage.getAttribute('data-full'),
                inPanel: panel.contains(stage),
                strays: document.querySelectorAll(
                    'body > [data-canvas-panel], #root > [data-canvas-panel]'
                ).length,
            };
        }"""
    )

    if placement["full"] != "false":
        raise AssertionError("сцена осталась развёрнутой")
    if placement["inPanel"] is not True:
        raise AssertionError("сцена не вернулась в панель")
    if placement["strays"] != 0:
        raise AssertionError("в корне остался осиротевший узел сцены")


async def test_zoom_survives_expanding(panel: Any) -> None:
    """Разворачивание не сбрасывает состояние вьювера: масштаб переживает его."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    read = (
        "() => document.querySelector("
        "'[data-canvas-stage] div[style*=\"transform-origin\"]').style.transform"
    )

    await side.locator('button[aria-label="Zoom in"]').first.click()
    before = await page.evaluate(read)

    await _expand(side)
    expanded = await page.evaluate(read)
    await _collapse(page)
    collapsed = await page.evaluate(read)

    if "scale(1.25)" not in before:
        raise AssertionError('"scale(1.25)" in before')
    if expanded != before:
        raise AssertionError("масштаб сбросился при разворачивании")
    if collapsed != before:
        raise AssertionError("масштаб сбросился при сворачивании")


async def test_panel_never_scrolls_horizontally(panel: Any) -> None:
    """Содержимое не выталкивает панель вбок: горизонтальной прокрутки нет."""
    show, _, _thread, _act = panel

    for name in ("flow.mmd", "report.md", "chart.png"):
        side = await show(name)
        overflow = await side.page.evaluate(
            """() => {
                const panel = document.querySelector('#side-view-content');
                return panel.scrollWidth - panel.clientWidth;
            }"""
        )

        if overflow > 2:
            raise AssertionError(f"{name}: панель прокручивается вбок на {overflow}px")


# ——— Вид: слои, фон, раскладка ———


async def test_fullscreen_covers_the_chat_underneath(panel: Any) -> None:
    """Полный экран перекрывает ленту: клик в центре попадает в сцену."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    await _expand(side)
    hit = await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-panel][data-full="true"]');
            const found = document.elementFromPoint(
                window.innerWidth / 2, window.innerHeight / 2);
            const style = getComputedStyle(stage);
            return {
                inside: stage.contains(found),
                background: style.backgroundColor,
                zIndex: style.zIndex,
            };
        }"""
    )
    await _collapse(page)

    if hit["inside"] is not True:
        raise AssertionError("лента перекрывает полноэкранную сцену")
    if hit["background"] in ("rgba(0, 0, 0, 0)", "transparent"):
        raise AssertionError("фон полноэкранной сцены прозрачный")
    if int(hit["zIndex"]) < 10:
        raise AssertionError(f"z-index сцены слишком мал: {hit['zIndex']}")


async def test_fullscreen_body_fills_the_height(panel: Any) -> None:
    """Тело занимает всё под шапкой: содержимое не жмётся в верхнюю полосу."""
    show, _, _thread, _act = panel
    side = await show("report.md")
    page = side.page

    await _expand(side)
    layout = await page.evaluate(
        """() => {
            // шапку узнаём по строке статуса, тело — следующий за ней блок
            const status = document.querySelector(
              '[data-canvas-panel][data-full="true"] [data-canvas-status]');
            const bar = status.parentElement;
            const body = bar.nextElementSibling;
            const barBox = bar.getBoundingClientRect();
            const bodyBox = body.getBoundingClientRect();
            return {
                barHeight: barBox.height,
                bodyHeight: bodyBox.height,
                bodyTop: Math.round(bodyBox.top - barBox.bottom),
                winHeight: window.innerHeight,
            };
        }"""
    )
    await _collapse(page)

    if layout["barHeight"] <= 0:
        raise AssertionError("шапка не видна в полноэкранном режиме")
    if abs(layout["bodyTop"]) > 2:
        raise AssertionError("тело не примыкает к шапке")
    expected = layout["winHeight"] - layout["barHeight"]
    if layout["bodyHeight"] < expected * 0.95:
        raise AssertionError("тело не занимает высоту под шапкой")


async def test_image_fits_without_overflow(panel: Any) -> None:
    """Картинка вписана в панель, а не торчит за её границами."""
    show, _, _thread, _act = panel
    side = await show("chart.png")

    fits = await side.page.evaluate(
        """() => {
            const img = document.querySelector('#side-view-content img');
            const box = img.getBoundingClientRect();
            const panel = document.querySelector(
              '#side-view-content').getBoundingClientRect();
            return box.width <= panel.width + 2 && box.height <= panel.height + 2;
        }"""
    )

    if fits is not True:
        raise AssertionError("картинка выходит за границы панели")


async def test_log_is_monospaced(panel: Any) -> None:
    """Журнал показывается моноширинным: колонки лога не разъезжаются."""
    show, _, _thread, _act = panel
    side = await show("notes.log")

    family = await side.page.evaluate(
        """() => getComputedStyle(
            document.querySelector('#side-view-content pre')).fontFamily"""
    )

    if "mono" not in family.lower():
        raise AssertionError(f"шрифт журнала не моноширинный: {family}")


# ——— Поведение: обновление файлов по сигналу слежения ———


async def test_markdown_reloads_when_the_file_changes(panel: Any) -> None:
    """Переписанный markdown перечитывается панелью без её переоткрытия."""
    show, _, thread, _act = panel
    side = await show("report.md")
    page = side.page

    await page.evaluate(
        """() => {
            document.querySelector(
              '#side-view-content [data-canvas-stage]').dataset.stamp = 'alive';
        }"""
    )

    await _rewrite(thread, "report.md", "# Отчёт\n\n- прибыль: 1 234\n".encode())

    updated = False
    for _ in range(24):
        if "прибыль" in await side.inner_text():
            updated = True
            break
        await page.wait_for_timeout(500)

    stamp = await page.evaluate(
        """() => document.querySelector(
            '#side-view-content [data-canvas-stage]').dataset.stamp || ''"""
    )

    if not updated:
        raise AssertionError("markdown не перечитался по сигналу слежения")
    if stamp != "alive":
        raise AssertionError("панель пересоздалась при обновлении markdown")


async def test_image_reloads_when_the_file_changes(panel: Any) -> None:
    """Перерисованная картинка подтягивается: ссылка получает метку ревизии."""
    show, _, thread, _act = panel
    side = await show("chart.png")
    page = side.page

    src_of = "() => document.querySelector('#side-view-content img').src"
    before = await page.evaluate(src_of)

    # тот же валидный PNG: меняется только время записи, и этого довольно —
    # версия файла считается по размеру вместе с ним
    await _rewrite(thread, "chart.png", PNG)

    changed = False
    for _ in range(24):
        if await page.evaluate(src_of) != before:
            changed = True
            break
        await page.wait_for_timeout(500)

    if not changed:
        raise AssertionError("ссылка картинки не обновилась по сигналу")

    # смена src запускает загрузку заново: ждём её, а не спрашиваем мгновенно
    await page.wait_for_function(
        """() => {
            const img = document.querySelector('#side-view-content img');
            return !!img && img.complete && img.naturalWidth > 0;
        }""",
        timeout=15000,
    )


# ——— Поведение: окна текста в панели (файл workspace, не журнал) ———


async def test_workspace_log_walks_by_windows(panel: Any) -> None:
    """Текстовый файл workspace листается окнами: «в конец» и «в начало»."""
    show, _, thread, _act = panel
    await _rewrite(
        thread,
        "notes.log",
        "".join(f"N{index:06d},row\n" for index in range(120000)).encode(),
    )
    side = await show("notes.log")
    page = side.page

    head = await side.inner_text()

    await side.locator('button[aria-label*="Go to the file end"]').first.click()
    await page.wait_for_timeout(1500)
    tail = await side.inner_text()

    await side.locator('button[aria-label="Go to the file start"]').first.click()
    await page.wait_for_timeout(1500)
    back = await side.inner_text()

    if "N000000,row" not in head:
        raise AssertionError("файл открылся не с начала")
    if "N119999,row" not in tail:
        raise AssertionError("«в конец» не показал хвост файла")
    if "N000000,row" in tail:
        raise AssertionError("хвост показан вместе с началом файла")
    if "N000000,row" not in back:
        raise AssertionError("«в начало» не вернуло к началу файла")


async def test_scrolling_up_loads_previous_window(panel: Any) -> None:
    """Прокрутка вверх подтягивает предыдущее окно и не дёргает позицию."""
    show, _, _thread, _act = panel
    side = await show("notes.log")
    page = side.page

    await side.locator('button[aria-label*="Go to the file end"]').first.click()
    await page.wait_for_timeout(1500)

    before = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            return box.querySelector('pre').textContent.length;
        }"""
    )

    grew = False
    for _ in range(8):
        await page.evaluate(
            """() => {
                const box = document.querySelector(
                  '#side-view-content [data-canvas-scroll]');
                box.scrollTop = 0;
            }"""
        )
        await page.wait_for_timeout(1200)
        after = await page.evaluate(
            """() => {
                const box = document.querySelector(
                  '#side-view-content [data-canvas-scroll]');
                return box.querySelector('pre').textContent.length;
            }"""
        )
        if after > before:
            grew = True
            break

    if not grew:
        raise AssertionError("прокрутка вверх не подтянула предыдущее окно")


async def test_font_size_survives_expanding(panel: Any) -> None:
    """Размер шрифта переживает разворачивание: состояние сцены одно."""
    show, _, _thread, _act = panel
    side = await show("notes.log")
    page = side.page

    read = "() => document.querySelector('[data-canvas-stage] pre').style.fontSize"

    await page.locator('button[aria-label="Larger"]').first.click()
    await page.locator('button[aria-label="Larger"]').first.click()
    before = await page.evaluate(read)

    await _expand(side)
    expanded = await page.evaluate(read)
    await _collapse(page)

    if before != "16px":
        raise AssertionError(f'before == "16px", получено {before}')
    if expanded != before:
        raise AssertionError("размер шрифта сбросился при разворачивании")


async def test_scroll_position_survives_expanding(panel: Any) -> None:
    """Прокрутка не прыгает при разворачивании: узел сцены тот же самый."""
    show, _, thread, _act = panel
    await _rewrite(
        thread,
        "notes.log",
        "".join(f"S{index:06d},row\n" for index in range(20000)).encode(),
    )
    side = await show("notes.log")
    page = side.page

    scrollable = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            box.scrollTop = 400;
            return box.scrollHeight > box.clientHeight;
        }"""
    )
    if scrollable is not True:
        raise AssertionError("содержимое короче окна: прокрутку не проверить")
    await page.wait_for_timeout(400)

    await _expand(side)
    kept = await page.evaluate(
        """() => document.querySelector(
            '[data-canvas-panel][data-full="true"] [data-canvas-scroll]').scrollTop"""
    )
    await _collapse(page)

    if abs(kept - 400) > 4:
        raise AssertionError(f"прокрутка съехала при разворачивании: {kept}")


# ——— Поведение: навигация и жизненный цикл панели ———


async def test_wheel_zooms_in_fullscreen(panel: Any) -> None:
    """На весь экран колесо зумит — в отличие от панели, где оно листает."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    await _expand(side)
    read = (
        "() => document.querySelector("
        "'[data-canvas-stage] div[style*=\"transform-origin\"]').style.transform"
    )
    before = await page.evaluate(read)

    viewport = page.viewport_size
    await page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
    await page.mouse.wheel(0, -400)
    await page.wait_for_timeout(400)
    after = await page.evaluate(read)

    # в полном экране сцена живёт в body: кнопки ищем на странице, не в панели
    await page.locator('button[aria-label="Reset view"]').first.click()
    await _collapse(page)

    if after == before:
        raise AssertionError("колесо не зумит в полноэкранном режиме")


async def test_diagram_pans_by_dragging(panel: Any) -> None:
    """Диаграмму можно таскать мышью: сцена сдвигается вслед за курсором."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    read = (
        "() => document.querySelector("
        "'#side-view-content div[style*=\"transform-origin\"]').style.transform"
    )
    before = await page.evaluate(read)

    box = await side.bounding_box()
    if not box:
        raise AssertionError("панель без геометрии")

    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(start_x - 120, start_y - 60, steps=8)
    await page.mouse.up()
    await page.wait_for_timeout(300)

    after = await page.evaluate(read)
    await side.locator('button[aria-label="Reset view"]').first.click()

    if after == before:
        raise AssertionError("перетаскивание не сдвинуло диаграмму")


async def test_panel_reopens_after_closing(panel: Any) -> None:
    """Закрытую панель можно открыть снова тем же действием."""
    show, _, _thread, _act = panel
    side = await show("report.md")
    page = side.page

    await side.locator('button[aria-label="Close"]').first.click()
    await page.wait_for_timeout(1500)
    if await page.locator("#side-view-content").count() != 0:
        raise AssertionError("панель не закрылась")

    reopened = await show("flow.mmd")
    if "Доход" not in await reopened.inner_text():
        raise AssertionError("панель не открылась повторно")


async def test_missing_file_is_explained(panel: Any) -> None:
    """Пропавший файл объясняется в панели, а не роняет её молча."""
    _show, _, thread, act = panel
    side = await act("canvas_open", {"path": _tool_view(thread, "нет-такого.md")})

    text = await side.inner_text()
    if not text.strip():
        raise AssertionError("панель промолчала о пропавшем файле")


async def test_theme_change_redraws_the_diagram(panel: Any) -> None:
    """Смена темы перерисовывает диаграмму: mermaid рисует свою палитру."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    read = """() => {
        const svg = document.querySelector(
          '#side-view-content div[style*="transform-origin"] svg');
        return svg ? svg.innerHTML.length : 0;
    }"""

    before = await page.evaluate(read)
    was_dark = await page.evaluate(
        "() => document.documentElement.classList.contains('dark')"
    )

    await page.evaluate("() => document.documentElement.classList.toggle('dark')")
    await page.wait_for_timeout(2500)
    after = await page.evaluate(read)

    if was_dark != await page.evaluate(
        "() => document.documentElement.classList.contains('dark')"
    ):
        await page.evaluate("() => document.documentElement.classList.toggle('dark')")
        await page.wait_for_timeout(1500)

    if before <= 0 or after <= 0:
        raise AssertionError("диаграмма пропала при смене темы")


async def test_buttons_work_in_fullscreen(panel: Any) -> None:
    """Кнопки в полном экране действительно работают, а не просто нарисованы.

    Сцена уезжает из панели, и если поднять её мимо корня React, клики
    перестают доходить до обработчиков — снаружи это выглядит как мёртвые
    кнопки. Проверяем результат клика, а не его факт.
    """
    show, _, _thread, _act = panel
    side = await show("flow.mmd")
    page = side.page

    await _expand(side)

    read = (
        "() => document.querySelector("
        '\'[data-canvas-panel][data-full="true"] '
        'div[style*="transform-origin"]\').style.transform'
    )
    before = await page.evaluate(read)

    await page.locator(
        '[data-canvas-panel][data-full="true"] button[aria-label="Zoom in"]'
    ).click()
    await page.wait_for_timeout(300)
    zoomed = await page.evaluate(read)

    await page.locator(
        '[data-canvas-panel][data-full="true"] button[aria-label="Reset view"]'
    ).click()
    await page.wait_for_timeout(300)
    reset = await page.evaluate(read)

    await page.locator(
        '[data-canvas-panel][data-full="true"] button[aria-label="Close"]'
    ).click()
    await page.wait_for_timeout(400)
    collapsed = await page.evaluate(
        """() => document.querySelector(
            '[data-canvas-panel]').getAttribute('data-full')"""
    )

    if zoomed == before:
        raise AssertionError("кнопка зума не работает в полном экране")
    if reset != before:
        raise AssertionError("сброс вида не работает в полном экране")
    if collapsed != "false":
        raise AssertionError("кнопка закрытия не сворачивает полный экран")


DOWNLOADABLE = ("chart.png", "report.md", "flow.mmd", "notes.log", "data.bin")
"""Все типы показа: картинка, markdown, диаграмма, лог и формат без вьювера."""


async def test_every_viewer_offers_download(panel: Any) -> None:
    """Кнопка скачивания есть у любого показанного файла: панель всегда о файле.

    Ссылку проставляет база вьюверов, а рисует кнопку общая шапка сцены —
    поэтому наследники получают её без собственного кода.
    """
    show, _, _thread, _act = panel

    for name in DOWNLOADABLE:
        side = await show(name)
        found = await side.locator('button[aria-label="Download file"]').count()

        if found != 1:
            raise AssertionError(f"{name}: кнопки скачивания нет (найдено {found})")


async def test_download_saves_the_shown_file(panel: Any) -> None:
    """Кнопка отдаёт именно показанный файл под его собственным именем."""
    show, _, _thread, _act = panel

    for name in ("chart.png", "flow.mmd", "report.md"):
        side = await show(name)

        async with side.page.expect_download() as pending:
            await side.locator('button[aria-label="Download file"]').click()

        saved = await pending.value
        if saved.suggested_filename != name:
            raise AssertionError(f"{name}: скачался как {saved.suggested_filename}")


async def test_download_works_in_fullscreen(panel: Any) -> None:
    """В полном экране кнопка та же и так же работает."""
    show, _, _thread, _act = panel
    side = await show("report.md")
    page = side.page

    await _expand(side)
    async with page.expect_download() as pending:
        await page.locator(
            '[data-canvas-stage] button[aria-label="Download file"]'
        ).click()
    saved = await pending.value
    await _collapse(page)

    if saved.suggested_filename != "report.md":
        raise AssertionError(f"скачался {saved.suggested_filename}")


async def test_preview_card_has_no_download_button(panel: Any) -> None:
    """Карточка в ленте не панель: у неё своя ссылка на исходник, не кнопка."""
    show, _, _thread, _act = panel
    side = await show("flow.mmd")

    in_feed = await side.page.evaluate(
        """() => document.querySelectorAll(
            '.message-content button[aria-label="Download file"]').length"""
    )

    if in_feed:
        raise AssertionError("в карточке ленты не должно быть кнопки скачивания")
