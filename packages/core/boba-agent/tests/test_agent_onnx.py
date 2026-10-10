"""Агент на локальной модели onnx (pytest -m integration): два хода без
отказов и обрыв по пределу длины ответа. Ответы недетерминированы, поэтому
проверяются инварианты журнала, а не содержимое.

Модель называет секция [agent_stand] стендового слоя conf/stand.toml.
Запуск только в tmux: ход занимает минуты.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self

import pytest
from agent_stand import Collected, Served
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.agent.agent import Agent, AgentParts
from boba.agent.events import TurnOutcome
from boba.agent.history import SessionId
from boba.agent.profile import CompactionSettings, ModelProfile, TurnLimits
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    AssistantRecord,
    AttachmentRecord,
    ReplyStopReason,
    UserRecord,
)
from boba.config import bind
from boba.history.jsonl import JsonlHistoryStore
from boba.llm.onnx.chat import OnnxProvider
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes

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


def agent(
    stand: AgentStand,
    root: Path,
    max_tokens: int,
    sink: Collected,
    system_turns: bool = False,
) -> Agent:
    model_profile = profile(stand, max_tokens, system_turns)
    providers = LlmProviders(LlmProviderTypes.installed())
    parts = AgentParts(
        profile=model_profile,
        limits=TurnLimits(retry_delays_sec=(0.0, 0.0, 0.0)),
        compaction=CompactionSettings(),
        system_prompt=SYSTEM_PROMPT,
        store=JsonlHistoryStore(root),
        model=providers.chat(model_profile.chat),
        sink=sink,
        version=VERSION,
    )

    return Agent(parts)


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


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
