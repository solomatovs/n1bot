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

from boba.chainlit.data.stream_journal import DirVault, StreamJournal, StreamKey
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.db.postgres import AsyncPostgresPool
from boba.settings import bind, build_app_config

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CALL_ID = "call_e2e_stream0001"
LIVE_CALL_ID = "call_e2e_live0001"
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
            "insert into {threads} (id, created_at, name, user_id) "
            "values (%(id)s, now(), %(name)s, %(user_id)s) "
            "on conflict (id) do nothing"
        ).format(threads=sql.Identifier(dl.db_schema, "threads"))

        elements_query = sql.SQL(
            "insert into {elements} "
            "(id, thread_id, for_id, type, name, display, props, mime) "
            "values (%(id)s, %(thread_id)s, %(for_id)s, 'custom', "
            "'CanvasStream', 'inline', %(props)s, 'application/json') "
            "on conflict (id) do nothing"
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
    recorder = journal.recorder(key, "bash", lambda: None, frozenset())
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
    live = journal.recorder(live_key, "bash", lambda: None, frozenset())
    for index in range(LIVE_LINES):
        live.feed(f"V{index:07d},row\n".encode())
    # живой журнал не закрывается: вызов «ещё идёт» с точки зрения чтения


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

    button = trigger.locator('[aria-label="Показать вывод инструмента"]')
    assert await button.count() == 1


async def test_click_opens_the_journal_from_the_start(stream_thread: Any) -> None:
    """Открытие потока — окно с offset 0: первая строка файла на экране."""
    page, _thread_id = stream_thread
    trigger = await _open_step(page)

    await trigger.locator('[aria-label="Показать вывод инструмента"]').click()
    await page.wait_for_timeout(3000)

    text = await page.locator("#side-view-content").inner_text()
    assert FIRST_LINE in text
    assert LAST_LINE not in text


async def test_jump_to_end_and_back(stream_thread: Any) -> None:
    """«В конец файла» показывает хвост, «в начало файла» возвращает к нулю."""
    page, _thread_id = stream_thread
    side = page.locator("#side-view-content")

    await side.locator('button[aria-label*="В конец файла"]').click()
    await page.wait_for_timeout(2000)
    tail = await side.inner_text()
    assert LAST_LINE in tail
    assert FIRST_LINE not in tail

    await side.locator('button[aria-label="В начало файла"]').click()
    await page.wait_for_timeout(2000)
    head = await side.inner_text()
    assert FIRST_LINE in head


async def test_panel_and_fullscreen_share_the_button_set(
    stream_thread: Any,
) -> None:
    """Набор кнопок совпадает; различие — только замыкающая (развернуть/закрыть)."""
    page, _thread_id = stream_thread
    side = page.locator("#side-view-content")

    labels = await page.evaluate(
        """() => [...document.querySelectorAll(
            '#side-view-content button[aria-label]'
        )].map(b => b.getAttribute('aria-label'))"""
    )

    await side.locator('button[aria-label="Во весь экран"]').click()
    await page.wait_for_timeout(1000)

    full_labels = await page.evaluate(
        """() => [...document.querySelector('[role="dialog"]')
            .querySelectorAll('button[aria-label]')]
            .filter(b => getComputedStyle(b).display !== 'none')
            .map(b => b.getAttribute('aria-label'))"""
    )
    await page.keyboard.press("Escape")

    # панель = полноэкранный набор плюс «Во весь экран»; «Закрыть» есть в обоих
    assert set(labels) - {"Во весь экран"} == set(full_labels)
    assert "Закрыть" in set(full_labels)


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
    await trigger.locator('[aria-label="Показать вывод инструмента"]').click()
    await page.wait_for_timeout(3000)

    side = page.locator("#side-view-content")
    assert FIRST_LINE in await side.inner_text()

    seen_l5000 = False
    for _ in range(6):
        await _scroll_to_bottom(page)
        text = await side.inner_text()
        if "L0005000,row" in text:
            seen_l5000 = True
            break

    assert seen_l5000, "прокрутка вниз не подгрузила следующее окно"


async def test_scrolling_down_works_on_a_live_journal(
    stream_thread: Any, panel: Any
) -> None:
    """Живой (незакрытый) журнал, открытый с начала, тоже листается вниз."""
    page, _thread_id = stream_thread

    act = panel[3]
    side = await act("canvas_stream", {"call_id": LIVE_CALL_ID})
    assert "V0000000,row" in await side.inner_text()

    seen = False
    for _ in range(6):
        await _scroll_to_bottom(page)
        if "V0005000,row" in await side.inner_text():
            seen = True
            break

    assert seen, "живой журнал не листается вниз"


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

    assert cache_control == "no-cache"


async def test_dom_stays_bounded_on_a_long_scroll(stream_thread: Any) -> None:
    """Фронт потоковый, как и бекенд: длинная прокрутка не копит файл в DOM.

    Цепочка окон подрезается с дальнего края — сколько ни мотай, в браузере
    живёт не больше горстки окон; верх загруженного уезжает от начала файла.
    """
    page, _thread_id = stream_thread
    trigger = await _open_step(page)
    await trigger.locator('[aria-label="Показать вывод инструмента"]').click()
    await page.wait_for_timeout(3000)

    side = page.locator("#side-view-content")
    assert FIRST_LINE in await side.inner_text()

    for _ in range(14):
        await _scroll_to_bottom(page)

    probe = await page.evaluate(
        """() => {
            const pre = document.querySelector('#side-view-content pre');
            const text = pre.textContent;
            return { length: text.length, head: text.slice(0, 20) };
        }"""
    )

    # 8 окон по 64 КиБ плюс запас на хвостовой добор
    assert probe["length"] < 9 * 66000, "DOM копит окна вместо вытеснения"
    assert FIRST_LINE not in probe["head"], "верх загруженного не вытеснился"
