"""Открытие треда в браузере: пока воспроизводится история, вкладка показывает
загрузку, а не идущий ход (кнопку Stop); тред другого профиля открывается
одним возвратом — без переподключения сокета и повторного воспроизведения.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from chat_ui import OpenChat
from playwright.sync_api import expect

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.socket_log import ChatEvent
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

ADMIN_LOGIN = "admin"


class OpenMark(StrEnum):
    """Разметка открытия треда, за которой следит запись кадров DOM."""

    STOP = "#stop-button"
    LOADING = "#history-loading"
    SUBMIT = "#chat-submit"
    FRAMES = "__bobaOpenFrames"
    RESUME = "resume"


@dataclass(frozen=True)
class DomFrame:
    """Состояние страницы на одной перестройке DOM: есть ли кнопка Stop,
    индикатор загрузки истории и сколько шагов в ленте."""

    stop: bool
    loading: bool
    steps: int


class OpenFrames:
    """Запись кадров DOM с начала загрузки страницы: MutationObserver, который
    скрипт инициализации ставит до разметки, кладёт в окно состояние на каждой
    перестройке, где оно поменялось."""

    SCRIPT: ClassVar[str] = f"""
        (() => {{
            const frames = [];
            window.{OpenMark.FRAMES.value} = frames;
            let last = '';
            const take = () => {{
                const frame = {{
                    stop: !!document.querySelector('{OpenMark.STOP.value}'),
                    loading: !!document.querySelector('{OpenMark.LOADING.value}'),
                    steps: document.querySelectorAll('{Selector.STEP.value}').length
                }};
                const key = JSON.stringify(frame);
                if (key !== last) {{
                    last = key;
                    frames.push(frame);
                }}
            }};
            new MutationObserver(take).observe(document, {{
                childList: true, subtree: true, attributes: true
            }});
        }})();
    """

    def __init__(self, chat: ChatPage) -> None:
        self._chat = chat

    def install(self) -> None:
        self._chat.page.add_init_script(self.SCRIPT)

    def taken(self) -> list[DomFrame]:
        raw: list[dict[str, Any]] = self._chat.page.evaluate(
            f"window.{OpenMark.FRAMES.value} || []"
        )
        frames: list[DomFrame] = []
        for item in raw:
            frames.append(
                DomFrame(
                    stop=bool(item["stop"]),
                    loading=bool(item["loading"]),
                    steps=int(item["steps"]),
                )
            )

        return frames


def _asked_thread(chat: ChatPage) -> tuple[str, str]:
    """Тред с одним ходом: id и текст вопроса, по которому тред находится."""
    probe = f"thread open probe {uuid4().hex[:8]}"
    chat.ask(f"{ScenarioName.ANSWER.value} {probe}")
    chat.await_step(StepKind.ASSISTANT.value)
    chat.await_idle()

    return chat.log.thread_id(), probe


def _resumes(chat: ChatPage) -> int:
    """Сколько раз сервер открыл вкладке тред возвратом (first_interaction
    resume): каждое переподключение сокета к треду — ещё один."""
    count = 0
    for frame in chat.log.of_event(ChatEvent.FIRST_INTERACTION):
        if not isinstance(frame.payload, dict):
            continue

        if frame.payload.get("interaction") == OpenMark.RESUME.value:
            count += 1

    return count


class TestHistoryLoading:
    """Пока история треда воспроизводится, вкладка показывает загрузку."""

    WAIT_SEC: ClassVar[float] = 60.0

    def test_loading_instead_of_stop_while_the_thread_opens(
        self, open_chat: OpenChat, stand: StandProcess
    ) -> None:
        author = open_chat(stand, ADMIN_LOGIN)
        thread_id, probe = _asked_thread(author)

        chat = open_chat(stand, ADMIN_LOGIN)
        frames = OpenFrames(chat)
        frames.install()
        chat.log.clear()
        chat.page.goto(
            f"{stand.config.base_url}/thread/{thread_id}",
            wait_until="domcontentloaded",
        )
        chat.await_event(ChatEvent.HISTORY_LOADED, self.WAIT_SEC)

        asked = chat.page.locator(Selector.of_type(StepKind.USER.value))
        expect(asked.filter(has_text=probe)).to_be_visible()
        expect(chat.page.locator(OpenMark.LOADING.value)).to_have_count(0)
        expect(chat.page.locator(OpenMark.SUBMIT.value)).to_be_visible()

        taken = frames.taken()
        stopped: list[DomFrame] = []
        for frame in taken:
            if frame.stop:
                stopped.append(frame)

        if stopped:
            raise AssertionError(
                f"the Stop button was shown while the thread opened: {taken}"
            )

        loading = False
        for frame in taken:
            if frame.loading:
                loading = True

        if not loading:
            raise AssertionError(
                f"no loading indicator while the thread opened: {taken}"
            )


class TestThreadOfAnotherProfile:
    """Тред другого профиля: фронт выравнивает профиль под тред без нового
    подключения, история воспроизводится один раз."""

    WAIT_SEC: ClassVar[float] = 60.0
    SAVE_WAIT_SEC: ClassVar[float] = 15.0
    SETTLE_MS: ClassVar[int] = 2000
    THREAD_HISTORY: ClassVar[str] = "#thread-history"
    GENERAL: ClassVar[str] = "Общий"

    def test_one_resume_when_the_profile_differs(
        self, open_chat: OpenChat, stand: StandProcess, stand_db: StandDatabase
    ) -> None:
        author = open_chat(stand, ADMIN_LOGIN)
        thread_id, probe = _asked_thread(author)
        self._left(author, stand_db, thread_id)

        chat = open_chat(stand, ADMIN_LOGIN)
        chat.open_profile_menu()
        chat.select_profile("search")
        chat.log.clear()

        entry = chat.page.locator(self.THREAD_HISTORY).get_by_text(probe)
        entry.click()
        chat.await_event(ChatEvent.HISTORY_LOADED, self.WAIT_SEC)
        chat.page.wait_for_timeout(self.SETTLE_MS)

        asked = chat.page.locator(Selector.of_type(StepKind.USER.value))
        expect(asked.filter(has_text=probe)).to_have_count(1)

        resumes = _resumes(chat)
        threads = len(chat.log.of_event(ChatEvent.RESUME_THREAD))
        loaded = len(chat.log.of_event(ChatEvent.HISTORY_LOADED))
        if (resumes, threads, loaded) != (1, 1, 1):
            raise AssertionError(
                f"opening a thread of another profile: {resumes} resume(s), "
                f"{threads} resume_thread, {loaded} history_loaded, expected one "
                f"each\n{chat.log.describe()}"
            )

        if chat.profile_label() != self.GENERAL:
            raise AssertionError(f"selector shows {chat.profile_label()!r}")

    def _left(self, author: ChatPage, stand_db: StandDatabase, thread_id: str) -> None:
        """Автор уходит со страницы: профиль треда chainlit пишет в meta на
        disconnect, и только с ним тред открывается «другим профилем»."""
        author.page.goto("about:blank")
        deadline = time.monotonic() + self.SAVE_WAIT_SEC
        while stand_db.thread_profile(thread_id) != "general":
            if time.monotonic() > deadline:
                saved = stand_db.thread_profile(thread_id)
                msg = (
                    f"thread {thread_id}: meta.chat_profile is {saved!r} "
                    f"{self.SAVE_WAIT_SEC}s after the author left, expected 'general'"
                )
                raise AssertionError(msg)

            time.sleep(0.2)
