"""Агент на локальной модели onnx (pytest -m integration): два хода без
отказов, обрыв по пределу длины ответа, порог сжатия на кириллическом
тексте. Ответы недетерминированы, поэтому проверяются инварианты журнала,
а не содержимое.

Модель называет секция [agent_stand] стендового слоя conf/stand.toml.
Запуск только в tmux: ход занимает минуты.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import ClassVar, Self

import pytest
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.agent.agent import Agent, AgentParts
from boba.agent.control import DenyAnswer
from boba.agent.events import TurnOutcome
from boba.agent.history import SessionId
from boba.agent.profile import CompactionSettings, ModelProfile, TurnLimits
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    AssistantRecord,
    AttachmentRecord,
    CompactBoundaryRecord,
    ReplyStopReason,
    UserRecord,
)
from boba.config import bind
from boba.history.jsonl import JsonlHistoryStore
from boba.llm.onnx.chat import OnnxProvider
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes
from boba.stand.agent import Collected, Served

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SESSION = SessionId(value="agent-onnx-1")
VERSION = "0.0.26.dev4"
SYSTEM_PROMPT = ("You are a terse assistant. Answer in one short sentence.",)


class AgentStand(BaseModel):
    """Секция [agent_stand]: каталог весов локальной модели и потолок ответа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    WEIGHTS: ClassVar[str] = "genai_config.json"

    model_dir: Path
    max_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _weights_are_placed(self) -> Self:
        manifest = self.model_dir / self.WEIGHTS
        if not manifest.is_file():
            msg = (
                f"[agent_stand] model_dir {self.model_dir}: expected the weights of "
                f"the local model with {self.WEIGHTS}, the file is missing"
            )
            raise ValueError(msg)

        return self


@pytest.fixture(scope="module")
def agent_stand(raw_config: DictConfig) -> AgentStand:
    return bind(raw_config, path="agent_stand", model=AgentStand)


def profile(
    stand: AgentStand, max_tokens: int, system_turns: bool = False
) -> ModelProfile:
    provider = OnnxProvider(kind="onnx", model_dir=str(stand.model_dir))

    return ModelProfile(
        chat=ChatModelConfig(
            provider=provider, model="qwen3", sampling={"max_tokens": max_tokens}
        ),
        context_window=8000,
        max_output_tokens=max_tokens,
        system_turns=system_turns,
    )


def agent(  # noqa: PLR0913 — стенд собирает агента по частям, как вход процесса
    stand: AgentStand,
    root: Path,
    max_tokens: int,
    sink: Collected,
    system_turns: bool = False,
    compaction: CompactionSettings | None = None,
) -> Agent:
    if compaction is None:
        compaction = CompactionSettings()

    model_profile = profile(stand, max_tokens, system_turns)
    providers = LlmProviders(LlmProviderTypes.installed())
    parts = AgentParts(
        profile=model_profile,
        limits=TurnLimits(retry_delays_sec=(0.0, 0.0, 0.0)),
        compaction=compaction,
        system_prompt=SYSTEM_PROMPT,
        store=JsonlHistoryStore(root),
        model=providers.chat(model_profile.chat),
        sink=sink,
        version=VERSION,
    )

    return Agent(parts)


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


SETTLE_TIMEOUT_SEC = 900.0


async def deny_questions(served: Served, sink: Collected) -> None:
    """Отвечает отказом на каждый вопрос агента наружу: на длинный промпт
    локальная модель порой отвечает вызовом AskUserQuestion, а без ответа ход
    ждал бы вечно."""
    seen = 0
    while True:
        try:
            question = await sink.next_question(seen)
        except TimeoutError:
            continue

        served.agent.answer(
            question.request_id, DenyAnswer(message="nobody answers questions here")
        )
        seen += 1


async def settled(served: Served) -> None:
    """Ждёт конца хода не дольше SETTLE_TIMEOUT_SEC; по истечении печатает
    стеки всех задач цикла событий — зависание на локальной модели иначе не
    разобрать — и роняет тест."""
    try:
        await asyncio.wait_for(served.agent.settled(), SETTLE_TIMEOUT_SEC)
    except TimeoutError:
        for task in asyncio.all_tasks():
            print(f"--- task {task.get_name()} ---", file=sys.stderr)
            task.print_stack(file=sys.stderr)

        pytest.fail(f"the turn did not settle in {SETTLE_TIMEOUT_SEC} s")


class TestLocalModel:
    @pytest.mark.parametrize("system_turns", [False, True])
    async def test_two_turns_keep_the_invariants(
        self, agent_stand: AgentStand, tmp_path: Path, system_turns: bool
    ) -> None:
        sink = Collected()
        served = Served(
            agent(agent_stand, tmp_path / "history", 256, sink, system_turns)
        )
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt("Say hello in one word."))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt("Now say goodbye in one word."))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 2
        chain = session.chain()
        previous: str | None = None
        for record in chain:
            assert record.parent_uuid == previous
            previous = record.uuid

        replies = [r for r in chain if isinstance(r, AssistantRecord)]
        assert replies
        # потолок в 256 токенов локальная модель с рассуждениями может задеть:
        # тогда перед ответом лежат частичные записи, их ids не считаются
        finals = [
            r for r in replies if r.message.stop_reason is ReplyStopReason.END_TURN
        ]
        assert len({r.message.id for r in finals}) == 2
        assert all(r.message.usage.input_tokens > 0 for r in finals)
        assert all(r.message.usage.output_tokens > 0 for r in finals)
        snapshots = [
            r
            for r in chain
            if isinstance(r, AttachmentRecord)
            and r.attachment.type == "prompt_snapshot"
        ]
        assert len(snapshots) == 1

    async def test_output_limit_gives_partials_meta_and_retries(
        self, agent_stand: AgentStand, tmp_path: Path
    ) -> None:
        sink = Collected()
        served = Served(agent(agent_stand, tmp_path / "history", 5, sink))
        session = await served.open(SESSION, resume=False)

        served.agent.queue.enqueue(prompt("Describe the solar system in detail."))
        await served.agent.settled()
        await served.close()

        assert list(served.agent.outcomes) == [TurnOutcome.API_ERROR]
        chain = session.chain()
        partials = [
            r
            for r in chain
            if isinstance(r, AssistantRecord)
            and r.message.stop_reason is ReplyStopReason.MAX_TOKENS
        ]
        metas = [r for r in chain if isinstance(r, UserRecord) and r.is_meta]
        assert len(metas) == 3
        # четыре попытки, в каждой не больше двух блоков: рассуждение и текст
        assert 1 <= len(partials) <= 8
        final = chain[-1]
        assert isinstance(final, AssistantRecord)
        assert final.is_api_error_message

    async def test_cyrillic_prompt_crosses_the_threshold_before_the_window(
        self,
        agent_stand: AgentStand,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Длинный кириллический промпт: оценка по знакам переводит ход за
        порог сжатия раньше, чем настоящее окно модели переполняется. Сама
        локальная модель пересказ с потолком в 256 токенов обычно не
        дописывает — тогда неудача считается, а ход продолжается без ошибок."""
        sink = Collected()
        settings = CompactionSettings(compact_at=0.4, block_at=0.95)
        served = Served(
            agent(agent_stand, tmp_path / "history", 256, sink, compaction=settings)
        )
        session = await served.open(SESSION, resume=False)
        answerer = asyncio.create_task(deny_questions(served, sink))

        sentence = "Погода сегодня тихая, и река несёт листья к старому мосту. "
        story = sentence * 150
        with caplog.at_level(logging.INFO, logger="boba.agent"):
            served.agent.queue.enqueue(prompt(f"{story}\nОтветь одним словом: да."))
            await settled(served)
            served.agent.queue.enqueue(prompt("Скажи «да»."))
            await settled(served)
            served.agent.queue.enqueue(prompt("Ещё раз скажи «да»."))
            await settled(served)
            await served.close()

        answerer.cancel()

        assert list(served.agent.outcomes) == [TurnOutcome.COMPLETED] * 3
        chain = session.chain()
        assert not any(
            isinstance(r, AssistantRecord) and r.is_api_error_message for r in chain
        )
        boundaries = [r for r in chain if isinstance(r, CompactBoundaryRecord)]
        failures = [m for m in caplog.messages if m.startswith("compaction failed")]
        attempts = [m for m in caplog.messages if m.startswith("compaction:")]
        assert len(boundaries) + len(failures) >= 2, (attempts, failures)
        if boundaries:
            summaries = [
                r for r in chain if isinstance(r, UserRecord) and r.is_compact_summary
            ]
            assert len(summaries) == len(boundaries)
