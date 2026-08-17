"""Поток инструмента в живом браузере: кнопка на шаге и прокрутка журнала.

Ход агента не гоняется: история треда с вызовом bash подкладывается прямо в
checkpointer, кнопка потока — в data layer, журнал вызова — в служебный том.
Дальше playwright открывает тред и проверяет DOM: кнопка стоит в строке
заголовка шага, клик открывает журнал с начала, «в конец»/«в начало»
перематывают, набор кнопок панели и полноэкранного режима совпадает.

Запуск: BOBA_CONFIG_PATH=... pytest -m integration tests/test_stream_e2e.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, MessagesState, StateGraph
from psycopg import sql
from psycopg.types.json import Jsonb
from test_canvas_e2e import (
    BASE,
    USER_ID,
    anyio_backend,
    app_server,
    panel,
)

__all__ = ["anyio_backend", "app_server", "panel"]

from boba.chainlit.canvas.journal import DirVault, StreamJournal, StreamKey
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.db.postgres import AsyncPostgresPool
from boba.settings import bind, build_app_config
from boba.toolkit.channels import ToolChannel

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CALL_ID = "call_e2e_stream0001"
LIVE_CALL_ID = "call_e2e_live0001"
SHORT_CALL_ID = "call_e2e_short0001"
LINES = 200000
LIVE_LINES = 15000
FIRST_LINE = "L0000000,start"
LAST_LINE = f"L{LINES - 1:07d},row"


def _config() -> AppConfig:
    raw = build_app_config(config_path=Path(os.environ["BOBA_CONFIG_PATH"]))
    return bind(raw, path="app", model=AppConfig)


async def _seed_history(config: AppConfig, thread_id: str) -> None:
    """История с вызовом bash — в checkpointer приложения."""
    cp = config.checkpointer
    pool = AsyncPostgresPool(
        cp.postgres, override_options={"search_path": cp.db_schema}
    )
    await pool.open()
    try:
        graph = StateGraph(MessagesState)
        graph.add_node("noop", lambda state: {})
        graph.add_edge(START, "noop")
        compiled = graph.compile(checkpointer=AsyncPostgresSaver(pool.raw))

        history = [
            HumanMessage(content="сгенерируй csv", id="q-1"),
            AIMessage(
                content="",
                id="ai-1",
                tool_calls=[
                    {
                        "name": "bash",
                        "args": {"command": "generate"},
                        "id": CALL_ID,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content='{"exit_code": 0}', tool_call_id=CALL_ID, id="tm-1"),
            AIMessage(content="готово", id="ai-2"),
        ]
        await compiled.ainvoke(
            {"messages": history},
            {"configurable": {"thread_id": thread_id}},
        )
    finally:
        await pool.close()


async def _seed_thread_and_button(config: AppConfig, thread_id: str) -> None:
    """Тред пользователя и элемент кнопки потока — в data layer."""
    dl = config.data_layer
    pool = AsyncPostgresPool(
        dl.postgres, override_options={"search_path": dl.db_schema}
    )
    await pool.open()
    try:
        threads_query = sql.SQL(
            """
            insert into {threads} (
                id,
                created_at,
                name,
                user_id
            )
            values (
                %(id)s,
                now(),
                %(name)s,
                %(user_id)s
            )
            on conflict (id) do nothing
            """
        ).format(threads=sql.Identifier(dl.db_schema, "threads"))

        elements_query = sql.SQL(
            """
            insert into {elements} (
                id,
                thread_id,
                for_id,
                type,
                name,
                display,
                props,
                mime
            )
            values (
                %(id)s,
                %(thread_id)s,
                %(for_id)s,
                'custom',
                'CanvasStream',
                'inline',
                %(props)s,
                'application/json'
            )
            on conflict (id) do nothing
            """
        ).format(elements=sql.Identifier(dl.db_schema, "elements"))

        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                threads_query,
                {
                    "id": uuid.UUID(thread_id),
                    "name": "e2e stream",
                    "user_id": int(USER_ID),
                },
            )
            await conn.execute(
                elements_query,
                {
                    "id": uuid.UUID(
                        str(ChatView.derive_id(thread_id, CALL_ID, StepRole.STREAM))
                    ),
                    "thread_id": uuid.UUID(thread_id),
                    "for_id": uuid.UUID(
                        str(ChatView.derive_id(thread_id, CALL_ID, StepRole.TOOL))
                    ),
                    "props": Jsonb({"call_id": CALL_ID, "label": "bash"}),
                },
            )
    finally:
        await pool.close()


def _seed_journals(config: AppConfig, thread_id: str) -> None:
    """Журналы вызовов — в служебный том до старта работы приложения с ним."""
    journal_cfg = config.stream_journal
    vault = DirVault(journal_cfg.dir)
    journal = StreamJournal(vault, reserve_bytes=0)

    key = StreamKey(user_id=USER_ID, thread_id=thread_id, call_id=CALL_ID)
    recorder = journal.recorder(
        key, "bash", ToolChannel.STDOUT, lambda: None, frozenset()
    )
    recorder.feed(f"{FIRST_LINE}\n".encode())
    chunk: list[str] = []
    for index in range(1, LINES):
        chunk.append(f"L{index:07d},row\n")
        if len(chunk) >= 5000:
            recorder.feed("".join(chunk).encode())
            chunk.clear()
    recorder.feed("".join(chunk).encode())
    recorder.close("rc=0")

    live_key = StreamKey(user_id=USER_ID, thread_id=thread_id, call_id=LIVE_CALL_ID)
    live = journal.recorder(
        live_key, "bash", ToolChannel.STDOUT, lambda: None, frozenset()
    )
    for index in range(LIVE_LINES):
        live.feed(f"V{index:07d},row\n".encode())
    # живой журнал не закрывается: вызов «ещё идёт» с точки зрения чтения

    # короткий живой журнал: содержимое не заполняет окно, прокручивать нечего
    short_key = StreamKey(user_id=USER_ID, thread_id=thread_id, call_id=SHORT_CALL_ID)
    short = journal.recorder(
        short_key, "bash", ToolChannel.STDOUT, lambda: None, frozenset()
    )
    for index in range(5):
        short.feed(f"H{index:05d},row\n".encode())


@pytest.fixture(scope="module")
async def stream_thread(panel: Any) -> tuple[Any, str]:
    """Тред с шагом bash, кнопкой потока и журналом; страница открыта на нём."""
    show, _, _thread, _act = panel
    del show

    config = _config()
    thread_id = str(uuid.uuid4())
    await _seed_history(config, thread_id)
    await _seed_thread_and_button(config, thread_id)
    _seed_journals(config, thread_id)

    page = await panel_page(panel)
    await page.goto(f"{BASE}/thread/{thread_id}")
    await page.wait_for_selector('[id^="step-"]', timeout=15000)
    return page, thread_id


async def panel_page(panel: Any) -> Any:
    """Страница браузера из фикстуры panel."""
    side = await panel[3]("canvas_content", {"path": "/probe"})
    return side.page


async def _open_step(page: Any) -> Any:
    """Раскрывает контейнер process и вложенный шаг bash; отдаёт его триггер."""
    process = page.locator('button[id^="step-process"]').first
    if await process.get_attribute("data-state") != "open":
        await process.click()
        await page.wait_for_timeout(500)

    trigger = page.locator('button[id^="step-"][id*="bash"]').first
    await trigger.wait_for(timeout=5000)
    if await trigger.get_attribute("data-state") != "open":
        await trigger.click()
        await page.wait_for_timeout(500)
    return trigger


async def test_stream_button_lives_in_the_step_header(stream_thread: Any) -> None:
    """Кнопка потока — внутри строки заголовка шага, а не в его содержимом."""
    page, _thread_id = stream_thread
    trigger = await _open_step(page)

    button = trigger.locator('[aria-label="Show tool output"]')
    if await button.count() != 1:
        raise AssertionError("await button.count() == 1")


async def test_click_opens_the_journal_from_the_start(stream_thread: Any) -> None:
    """Открытие потока — окно с offset 0: первая строка файла на экране."""
    page, _thread_id = stream_thread
    trigger = await _open_step(page)

    await trigger.locator('[aria-label="Show tool output"]').click()
    await page.wait_for_timeout(3000)

    text = await page.locator("#side-view-content").inner_text()
    if FIRST_LINE not in text:
        raise AssertionError("FIRST_LINE in text")
    if LAST_LINE in text:
        raise AssertionError("LAST_LINE not in text")


async def test_jump_to_end_and_back(stream_thread: Any) -> None:
    """«В конец файла» показывает хвост, «в начало файла» возвращает к нулю."""
    page, _thread_id = stream_thread
    side = page.locator("#side-view-content")

    await side.locator('button[aria-label*="Go to the file end"]').click()
    await page.wait_for_timeout(2000)
    tail = await side.inner_text()
    if LAST_LINE not in tail:
        raise AssertionError("LAST_LINE in tail")
    if FIRST_LINE in tail:
        raise AssertionError("FIRST_LINE not in tail")

    await side.locator('button[aria-label="Go to the file start"]').click()
    await page.wait_for_timeout(2000)
    head = await side.inner_text()
    if FIRST_LINE not in head:
        raise AssertionError("FIRST_LINE in head")


async def test_panel_and_fullscreen_share_the_button_set(
    stream_thread: Any,
) -> None:
    """Набор кнопок совпадает; различие — только замыкающая (развернуть/закрыть).

    Полноэкранный режим — CSS-оверлей того же DOM-узла: сцена помечается
    data-full, а не рисуется отдельным диалогом.
    """
    page, _thread_id = stream_thread
    side = page.locator("#side-view-content")

    labels = await page.evaluate(
        """() => [...document.querySelectorAll(
            '#side-view-content button[aria-label]'
        )].map(b => b.getAttribute('aria-label'))"""
    )

    await side.locator('button[aria-label="Fullscreen"]').click()
    await page.wait_for_timeout(500)

    full_labels = await page.evaluate(
        """() => [...document.querySelector('[data-canvas-stage][data-full="true"]')
            .querySelectorAll('button[aria-label]')]
            .map(b => b.getAttribute('aria-label'))"""
    )
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)

    shrunk = await page.evaluate(
        """() => document.querySelector(
            '[data-canvas-stage]'
        ).getAttribute('data-full')"""
    )

    # панель = полноэкранный набор плюс «Во весь экран»; «Закрыть» есть в обоих
    if set(labels) - {"Fullscreen"} != set(full_labels):
        raise AssertionError('set(labels) - {"Fullscreen"} == set(full_labels)')
    if "Close" not in set(full_labels):
        raise AssertionError('"Close" in set(full_labels)')
    if shrunk != "false":
        raise AssertionError("Escape сворачивает полноэкранный режим")


async def _scroll_box(page: Any) -> Any:
    return page.locator("#side-view-content .overflow-auto").first


async def _scroll_to_bottom(page: Any) -> None:
    await page.evaluate(
        """() => {
            const box = document.querySelector('#side-view-content .overflow-auto');
            box.scrollTop = box.scrollHeight;
        }"""
    )
    await page.wait_for_timeout(1500)


async def test_scrolling_down_loads_next_windows(stream_thread: Any) -> None:
    """Непрерывная прокрутка: у нижней кромки подгружается следующее окно."""
    page, _thread_id = stream_thread
    trigger = await _open_step(page)
    await trigger.locator('[aria-label="Show tool output"]').click()
    await page.wait_for_timeout(3000)

    side = page.locator("#side-view-content")
    if FIRST_LINE not in await side.inner_text():
        raise AssertionError("FIRST_LINE in await side.inner_text()")

    seen_l5000 = False
    for _ in range(6):
        await _scroll_to_bottom(page)
        text = await side.inner_text()
        if "L0005000,row" in text:
            seen_l5000 = True
            break

    if not (seen_l5000):
        raise AssertionError("прокрутка вниз не подгрузила следующее окно")


async def test_scrolling_down_works_on_a_live_journal(
    stream_thread: Any, panel: Any
) -> None:
    """Живой (незакрытый) журнал, открытый с начала, тоже листается вниз."""
    page, _thread_id = stream_thread

    act = panel[3]
    side = await act("canvas_stream", {"call_id": LIVE_CALL_ID})
    if "V0000000,row" not in await side.inner_text():
        raise AssertionError('"V0000000,row" in await side.inner_text()')

    seen = False
    for _ in range(6):
        await _scroll_to_bottom(page)
        if "V0005000,row" in await side.inner_text():
            seen = True
            break

    if not (seen):
        raise AssertionError("живой журнал не листается вниз")


async def test_elements_are_served_without_cache(stream_thread: Any) -> None:
    """Кастом-элементы отдаются с no-cache: правка .jsx видна без hard reload."""
    page, _thread_id = stream_thread

    cache_control = await page.evaluate(
        """async () => {
            const rootPath =
              document.querySelector('meta[property="og:root_path"]')?.content || "";
            const answer = await fetch(
              rootPath.replace(/\\/$/, "") + "/public/elements/CanvasStream.jsx"
            );
            return answer.headers.get("cache-control");
        }"""
    )

    if cache_control != "no-cache":
        raise AssertionError('cache_control == "no-cache"')


async def test_dom_stays_bounded_on_a_long_scroll(stream_thread: Any) -> None:
    """Фронт потоковый, как и бекенд: длинная прокрутка не копит файл в DOM.

    Цепочка окон подрезается с дальнего края — сколько ни мотай, в браузере
    живёт не больше горстки окон; верх загруженного уезжает от начала файла.
    """
    page, _thread_id = stream_thread
    trigger = await _open_step(page)
    await trigger.locator('[aria-label="Show tool output"]').click()
    await page.wait_for_timeout(3000)

    side = page.locator("#side-view-content")
    if FIRST_LINE not in await side.inner_text():
        raise AssertionError("FIRST_LINE in await side.inner_text()")

    for _ in range(14):
        await _scroll_to_bottom(page)

    probe = await page.evaluate(
        """() => {
            const pre = document.querySelector('#side-view-content pre');
            const text = pre.textContent;
            return { length: text.length, head: text.slice(0, 20) };
        }"""
    )

    # бюджет фронта — 2 окна по 64 КиБ; запас на транзитный тик подрезки
    if probe["length"] >= 3 * 66000:
        raise AssertionError("DOM копит окна вместо вытеснения")
    if FIRST_LINE in probe["head"]:
        raise AssertionError("верх загруженного не вытеснился")


def _append_live(config: AppConfig, thread_id: str, marker: str, lines: int) -> None:
    """Дописать строки в живой журнал: как это делает инструмент из песочницы."""
    journal = StreamJournal(DirVault(config.stream_journal.dir), reserve_bytes=0)
    key = StreamKey(user_id=USER_ID, thread_id=thread_id, call_id=LIVE_CALL_ID)
    recorder = journal.recorder(
        key, "bash", ToolChannel.STDOUT, lambda: None, frozenset()
    )

    chunk: list[str] = []
    for index in range(lines):
        chunk.append(f"{marker}{index:05d},row\n")
    recorder.feed("".join(chunk).encode())


async def _open_live_tail(panel: Any) -> Any:
    """Открывает живой журнал и мотает в конец: панель следит за хвостом."""
    act = panel[3]
    side = await act("canvas_stream", {"call_id": LIVE_CALL_ID})
    await side.locator('button[aria-label*="Go to the file end"]').click()
    await side.page.wait_for_timeout(2000)
    return side


STAGE = '[data-canvas-stage][data-full="true"], #side-view-content [data-canvas-stage]'
"""Активная сцена: развёрнутая живёт в body, свёрнутая — внутри панели."""


async def _stage_text(page: Any) -> str:
    """Текст активной сцены: в полном экране она живёт вне панели."""
    return await page.evaluate(
        """selector => {
            const stage = document.querySelector(selector);
            return stage ? stage.innerText : '';
        }""",
        STAGE,
    )


def _stage_button(page: Any, label: str) -> Any:
    """Кнопка активной сцены: в полном экране её нет внутри панели."""
    return page.locator(f'{STAGE}').last.locator(f'button[aria-label*="{label}"]')


async def _wait_text(side: Any, marker: str, timeout_sec: float = 15.0) -> bool:
    for _ in range(int(timeout_sec * 2)):
        if marker in await _stage_text(side.page):
            return True
        await side.page.wait_for_timeout(500)
    return False


class _WindowCalls:
    """Счётчик запросов окон: замирание у прокрутки вверх проверяется по сети."""

    def __init__(self, page: Any) -> None:
        self.count = 0
        page.on("request", self._on_request)

    def _on_request(self, request: Any) -> None:
        if request.method != "POST":
            return
        body = request.post_data or ""
        if "canvas_stream_window" in body:
            self.count += 1


async def test_live_tail_follows_new_output(stream_thread: Any, panel: Any) -> None:
    """Пользователь на конце файла: новые строки доливаются сами, окно у низа.

    Содержимое по сокету не едет — приходит сигнал слежения, фронт сам
    запрашивает окно после текущего и дописывает сегмент.
    """
    page, thread_id = stream_thread
    side = await _open_live_tail(panel)

    # штамп на DOM-узлах: пересоздание панели стёрло бы его
    await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            box.dataset.stamp = 'alive';
        }"""
    )

    _append_live(_config(), thread_id, "F", 40)

    if not await _wait_text(side, "F00039,row"):
        raise AssertionError("долив хвоста не пришёл в панель")

    state = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            return {
                stamp: box.dataset.stamp || '',
                pinned: box.scrollHeight - box.scrollTop - box.clientHeight < 60,
            };
        }"""
    )

    if state["stamp"] != "alive":
        raise AssertionError("панель пересоздалась при доливе (штамп потерян)")
    if state["pinned"] is not True:
        raise AssertionError("окно не прижато к низу при follow")


async def test_scrolled_up_view_is_frozen(stream_thread: Any, panel: Any) -> None:
    """Промотал вверх — окно замирает: ни данных, ни перерисовки, ни запросов.

    Сигналы слежения приходят, но фронт не тянет окна и не трогает DOM;
    докрутка обратно в самый низ возобновляет follow.
    """
    page, thread_id = stream_thread
    side = await _open_live_tail(panel)

    # уходим от хвоста: три экрана вверх
    await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            box.scrollTop = box.scrollTop - box.clientHeight * 3;
        }"""
    )
    await page.wait_for_timeout(500)

    frozen = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            const pre = box.querySelector('pre');
            return { scrollTop: box.scrollTop, length: pre.textContent.length };
        }"""
    )

    calls = _WindowCalls(page)
    _append_live(_config(), thread_id, "Z", 40)
    await page.wait_for_timeout(4000)

    after = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            const pre = box.querySelector('pre');
            return { scrollTop: box.scrollTop, length: pre.textContent.length };
        }"""
    )

    if after["scrollTop"] != frozen["scrollTop"]:
        raise AssertionError("окно дёрнулось при сигнале вне follow")
    if after["length"] != frozen["length"]:
        raise AssertionError("окно перерисовалось при сигнале вне follow")
    if "Z00039,row" in await _stage_text(page):
        raise AssertionError("данные долились без follow")
    if calls.count != 0:
        raise AssertionError("фронт запрашивал окна, стоя выше хвоста")

    # докрутка в самый низ возобновляет follow: хвост доливается
    await side.locator('button[aria-label*="Go to the file end"]').click()
    await page.wait_for_timeout(1500)
    if not await _wait_text(side, "Z00039,row"):
        raise AssertionError("follow не возобновился у нижней кромки")


async def test_fullscreen_survives_live_output(stream_thread: Any, panel: Any) -> None:
    """Полный экран не сворачивается при доливе: рендерится только содержимое."""
    page, thread_id = stream_thread
    side = await _open_live_tail(panel)

    await side.locator('button[aria-label="Fullscreen"]').click()
    await page.wait_for_timeout(500)

    await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-stage][data-full="true"]');
            stage.dataset.stamp = 'alive';
        }"""
    )

    _append_live(_config(), thread_id, "W", 40)

    if not await _wait_text(side, "W00039,row"):
        raise AssertionError("долив не дошёл до полноэкранного режима")

    state = await page.evaluate(
        """() => {
            const stage = document.querySelector('[data-canvas-stage]');
            return {
                full: stage.getAttribute('data-full'),
                stamp: stage.dataset.stamp || '',
            };
        }"""
    )
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)

    if state["full"] != "true":
        raise AssertionError("полный экран свернулся при доливе")
    if state["stamp"] != "alive":
        raise AssertionError("полноэкранная сцена пересоздалась при доливе")


async def test_font_buttons_resize_the_text(stream_thread: Any, panel: Any) -> None:
    """Кнопки размера шрифта: больше, меньше, сброс — на pre окна."""
    page, _thread_id = stream_thread
    side = await _open_live_tail(panel)

    read = "() => document.querySelector('#side-view-content pre').style.fontSize"

    base = await page.evaluate(read)
    await side.locator('button[aria-label="Larger"]').click()
    bigger = await page.evaluate(read)
    await side.locator('button[aria-label="Smaller"]').click()
    await side.locator('button[aria-label="Smaller"]').click()
    smaller = await page.evaluate(read)
    await side.locator('button[aria-label="Reset size"]').click()
    reset = await page.evaluate(read)

    if base != "12px":
        raise AssertionError('base == "12px"')
    if bigger != "14px":
        raise AssertionError('bigger == "14px"')
    if smaller != "10px":
        raise AssertionError('smaller == "10px"')
    if reset != "12px":
        raise AssertionError('reset == "12px"')


async def test_download_button_saves_the_journal(
    stream_thread: Any, panel: Any
) -> None:
    """Скачивание отдаёт файл журнала файловым роутом, не через DOM."""
    page, _thread_id = stream_thread
    side = await _open_live_tail(panel)

    async with page.expect_download() as pending:
        await side.locator('button[aria-label="Download output"]').click()

    download = await pending.value
    name = download.suggested_filename

    if LIVE_CALL_ID not in name:
        raise AssertionError("LIVE_CALL_ID in name")
    if not name.endswith(".log"):
        raise AssertionError('name.endswith(".log")')


async def test_status_line_shows_the_window_position(
    stream_thread: Any, panel: Any
) -> None:
    """Статусная строка: границы окна, размер файла и running у живого вызова."""
    _page, _thread_id = stream_thread
    act = panel[3]
    side = await act("canvas_stream", {"call_id": LIVE_CALL_ID})

    status = await side.locator("[data-canvas-status]").inner_text()

    if " / " not in status:
        raise AssertionError('" / " in status')
    if "running" not in status:
        raise AssertionError('"running" in status')


async def test_live_follow_works_in_fullscreen(stream_thread: Any, panel: Any) -> None:
    """В полном экране слежение живёт: хвост доливается, окно остаётся у низа.

    Проверяется реальная геометрия, а не только пометка разворачивания:
    сцена обязана занимать всё окно и во время долива.
    """
    page, thread_id = stream_thread
    side = await _open_live_tail(panel)

    await side.locator('button[aria-label="Fullscreen"]').first.click()
    await page.wait_for_timeout(500)

    _append_live(_config(), thread_id, "G", 40)
    if not await _wait_text(side, "G00039,row"):
        raise AssertionError("хвост не долился в полноэкранном режиме")

    state = await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-stage][data-full="true"]');
            if (!stage) return null;
            const box = stage.getBoundingClientRect();
            const scroll = stage.querySelector('[data-canvas-scroll]');
            return {
                width: box.width,
                height: box.height,
                winWidth: window.innerWidth,
                winHeight: window.innerHeight,
                pinned:
                    scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60,
            };
        }"""
    )
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)

    if state is None:
        raise AssertionError("полный экран свернулся при доливе")
    if state["width"] < state["winWidth"] * 0.98:
        raise AssertionError(
            f'ширина {state["width"]} вместо {state["winWidth"]}'
        )
    if state["height"] < state["winHeight"] * 0.98:
        raise AssertionError(
            f'высота {state["height"]} вместо {state["winHeight"]}'
        )
    if state["pinned"] is not True:
        raise AssertionError("окно отлипло от низа при доливе в полном экране")


async def test_journal_fullscreen_covers_the_window(stream_thread: Any) -> None:
    """Журнал вызова тоже разворачивается на всё окно, а не в рамку панели."""
    page, _thread_id = stream_thread
    trigger = await _open_step(page)
    await trigger.locator('[aria-label="Show tool output"]').click()
    await page.wait_for_timeout(3000)

    await page.locator('button[aria-label="Fullscreen"]').first.click()
    await page.wait_for_timeout(500)

    geometry = await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-stage][data-full="true"]');
            if (!stage) return null;
            const box = stage.getBoundingClientRect();
            return {
                width: box.width,
                height: box.height,
                winWidth: window.innerWidth,
                winHeight: window.innerHeight,
                hosted: stage.parentElement === (
                    document.getElementById('root') || document.body),
            };
        }"""
    )
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)

    if geometry is None:
        raise AssertionError("журнал не развернулся")
    if geometry["width"] < geometry["winWidth"] * 0.98:
        raise AssertionError("журнал развернулся не на всю ширину окна")
    if geometry["height"] < geometry["winHeight"] * 0.98:
        raise AssertionError("журнал развернулся не на всю высоту окна")
    if geometry["hosted"] is not True:
        raise AssertionError("сцена журнала поднята не в корень приложения")


async def test_journal_windows_are_walkable_in_fullscreen(
    stream_thread: Any,
) -> None:
    """В полном экране журнал листается теми же кнопками, что и в панели."""
    page, _thread_id = stream_thread
    trigger = await _open_step(page)
    await trigger.locator('[aria-label="Show tool output"]').click()
    await page.wait_for_timeout(3000)

    await page.locator('button[aria-label="Fullscreen"]').first.click()
    await page.wait_for_timeout(500)

    await _stage_button(page, "Go to the file end").first.click()
    await page.wait_for_timeout(2000)
    tail = await _stage_text(page)

    await _stage_button(page, "Go to the file start").first.click()
    await page.wait_for_timeout(2000)
    head = await _stage_text(page)

    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)

    if LAST_LINE not in tail:
        raise AssertionError("«в конец» не сработало в полном экране")
    if FIRST_LINE not in head:
        raise AssertionError("«в начало» не сработало в полном экране")


def _append_short(config: AppConfig, thread_id: str, marker: str, lines: int) -> None:
    """Дописать строки в короткий живой журнал."""
    journal = StreamJournal(DirVault(config.stream_journal.dir), reserve_bytes=0)
    key = StreamKey(user_id=USER_ID, thread_id=thread_id, call_id=SHORT_CALL_ID)
    recorder = journal.recorder(
        key, "bash", ToolChannel.STDOUT, lambda: None, frozenset()
    )

    chunk: list[str] = []
    for index in range(lines):
        chunk.append(f"{marker}{index:05d},row\n")
    recorder.feed("".join(chunk).encode())


async def test_short_live_output_streams_without_scrolling(
    stream_thread: Any, panel: Any
) -> None:
    """Короткий живой вывод доливается сам, хотя прокручивать нечего.

    Содержимое не заполняет окно, поэтому события прокрутки не происходит
    вовсе — следование за хвостом не должно зависеть от того, поскроллил
    ли пользователь: окно и так показывает конец файла.
    """
    page, thread_id = stream_thread
    act = panel[3]

    side = await act("canvas_stream", {"call_id": SHORT_CALL_ID})
    if "H00000,row" not in await _stage_text(page):
        raise AssertionError("короткий журнал не открылся")

    _append_short(_config(), thread_id, "T", 20)

    if not await _wait_text(side, "T00019,row"):
        raise AssertionError("живой вывод не долился без прокрутки пользователем")


async def test_growing_live_output_keeps_catching_up(
    stream_thread: Any, panel: Any
) -> None:
    """Растущий поток догоняется раз за разом, а не залипает после первой порции.

    Сценарий пользователя: инструмент пишет непрерывно, панель открыта с
    начала файла и пользователь стоит у нижней кромки. Окно обязано ехать
    за хвостом на каждой порции — раньше оно отставало от размера файла и
    больше не догоняло, показывая старые строки при растущем счётчике.
    """
    page, thread_id = stream_thread
    act = panel[3]

    side = await act("canvas_stream", {"call_id": SHORT_CALL_ID})
    if "H00000,row" not in await _stage_text(page):
        raise AssertionError("короткий журнал не открылся")

    config = _config()
    for round_index in range(4):
        _append_short(config, thread_id, f"R{round_index}", 25)
        marker = f"R{round_index}00024,row"
        if not await _wait_text(side, marker, timeout_sec=20.0):
            raise AssertionError(
                f"поток залип на порции {round_index}: {marker} не пришло"
            )

    status = await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-stage][data-full="true"], '
              + '#side-view-content [data-canvas-stage]');
            return stage.querySelector('[data-canvas-status]').innerText;
        }"""
    )

    bounds, _, total = status.partition(" / ")
    end = int(bounds.split("–")[1])
    size = int(total.split()[0])
    if end != size:
        raise AssertionError(f"окно отстало от файла: {end} из {size}")
