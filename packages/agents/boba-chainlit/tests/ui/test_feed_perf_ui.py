"""SLA ленты под стримом: длинный тред не должен дорожать фронту с каждым ходом.

Длинные ходы меряются дважды — на коротком треде и после того, как лента набрала
длину короткими ходами; главный порог — отношение стоимости хода после к до:
он не зависит от загрузки хоста. Метрики через CDP: JS-время на socket-кадр,
heap после GC, число кадров на ход. Пороги сняты после патча фронта
(web/chainlit-ui) и серверной склейки токенов; на вендорном фронте тест обязан
падать. Цифры до и после — в docs/chainlit-frontend-patch.md.
"""

from __future__ import annotations

import time
from enum import Enum, IntEnum, unique

import pytest

from boba.stand.ui.chat_page import ChatPage
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.perf import PageMeter, TurnSample, TurnSeries
from boba.stand.ui.socket_log import ChatEvent

pytestmark = pytest.mark.ui


@unique
class Turns(IntEnum):
    """Сколько ходов какого рода делает тест."""

    MEASURED = 6
    """Длинные ходы на каждом замере: до разогрева и после."""

    WARMUP = 40
    """Короткие ходы, которыми лента набирает длину между замерами."""


@unique
class FeedSla(float, Enum):
    """Пороги SLA; главный — рост стоимости хода с длиной ленты."""

    LONG_THREAD_GROWTH = 1.25
    FRAMES_PER_TURN = 90
    """Токенов в ходе ~110 при кадре раз в 50 мс и токене раз в 30 мс."""

    SCRIPT_MS_PER_FRAME = 12.0
    HEAP_KB_PER_TURN = 900
    FRAMES_AFTER_STOP = 40
    """Кадры, дорисованные после Stop: очередь принятых плюс хвост сервера."""


class TestLongThread:
    """Одинаковые длинные ходы на короткой и длинной ленте стоят одинаково."""

    def test_turn_cost_stays_flat(self, chat: ChatPage) -> None:
        meter = PageMeter(chat.page)
        chat.page.wait_for_timeout(500)

        short = TurnSeries(self._long_turns(chat, meter, "short"))

        for turn in range(1, int(Turns.WARMUP) + 1):
            chat.ask(f"{ScenarioName.ANSWER.value} warmup {turn}")
            chat.await_idle(timeout_sec=120)

        long = TurnSeries(self._long_turns(chat, meter, "long"))

        growth = long.mean_script_ms() / short.mean_script_ms()
        per_frame_ms = long.max_script_ms_per_frame()
        heap_kb = long.heap_kb_per_turn()
        summary = (
            f"growth x{growth:.2f}, js/frame max {per_frame_ms:.1f} ms, "
            f"frames max {long.max_frames()}, heap {heap_kb:.0f} KB/turn"
        )
        threads = f"short thread:\n{short.describe()}\nlong thread:\n{long.describe()}"
        report = f"{summary}\n{threads}"
        print(f"\n{report}")

        if growth > FeedSla.LONG_THREAD_GROWTH:
            limit = FeedSla.LONG_THREAD_GROWTH.value
            msg = f"turn cost grew x{growth:.2f} > x{limit}\n{report}"
            raise AssertionError(msg)

        if long.max_frames() > FeedSla.FRAMES_PER_TURN:
            limit = FeedSla.FRAMES_PER_TURN.value
            msg = f"frames per turn {long.max_frames()} > {limit}\n{report}"
            raise AssertionError(msg)

        if per_frame_ms > FeedSla.SCRIPT_MS_PER_FRAME:
            limit = FeedSla.SCRIPT_MS_PER_FRAME.value
            msg = f"js per frame {per_frame_ms:.1f} ms > {limit}\n{report}"
            raise AssertionError(msg)

        if heap_kb > FeedSla.HEAP_KB_PER_TURN:
            limit = FeedSla.HEAP_KB_PER_TURN.value
            msg = f"heap grows {heap_kb:.0f} KB per turn > {limit}\n{report}"
            raise AssertionError(msg)

    @staticmethod
    def _long_turns(chat: ChatPage, meter: PageMeter, label: str) -> list[TurnSample]:
        before = meter.snapshot()
        samples: list[TurnSample] = []
        for turn in range(1, int(Turns.MEASURED) + 1):
            started = time.monotonic()
            chat.ask(f"{ScenarioName.LONG.value} {label} {turn}")
            chat.await_idle(timeout_sec=120)
            wall = time.monotonic() - started
            chat.page.wait_for_timeout(300)

            after = meter.snapshot()
            frames = len(chat.log.frames)
            samples.append(TurnSample.between(turn, wall, frames, before, after))
            before = after

        return samples


class TestStop:
    """Stop посреди стрима: лента не дорисовывает длинный хвост токенов."""

    STOP_BUTTON = "#stop-button"
    TOKENS_BEFORE_STOP = 5

    def test_few_frames_after_stop(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.LONG.value} stop")
        self._await_tokens(chat)

        seen = len(chat.log.frames)
        chat.page.locator(self.STOP_BUTTON).click()
        chat.await_idle(timeout_sec=60)
        chat.page.wait_for_timeout(500)

        after_stop = len(chat.log.frames) - seen
        print(f"\nframes after stop: {after_stop}")
        if after_stop > FeedSla.FRAMES_AFTER_STOP:
            raise AssertionError(
                f"{after_stop} frames after stop > {FeedSla.FRAMES_AFTER_STOP.value}\n"
                f"{chat.log.describe()}"
            )

    def _await_tokens(self, chat: ChatPage, timeout_sec: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            tokens = chat.log.of_event(ChatEvent.STREAM_CHUNK)
            if len(tokens) >= self.TOKENS_BEFORE_STOP:
                return

            chat.page.wait_for_timeout(50)

        raise AssertionError(f"stream did not start\n{chat.log.describe()}")
