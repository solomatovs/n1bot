"""Вызов инструмента глазами чата: шаг ленты, журнал канала и сообщение
истории.

ChatCalls проводит вызов узла настоящим исполнителем (LocalDagService) и
сообщает владельцу хода начало и итог вызова — по ним рисуется шаг ленты.
ToolRunLogger подключает приёмники каналов вызова. LangchainMessages
переводит итог вызова в сообщение инструмента истории и обратно.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import pytest
from chainlit_stand import FakeThreadMessages, FakeTurn
from langchain_core.messages import ToolMessage
from pydantic import Field

from boba.chainlit.agent.bridge import LangchainMessages
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.rendering.mount import ChatCalls, ChatMount
from boba.connection_broker.sealing import SentConnections
from boba.identity.run import Runs
from boba.runtime.journal import DirVault, StreamJournal
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.calls import ToolIntent
from boba.toolkit.chain import CallAmbient
from boba.toolkit.channels import ToolChannel
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NodeOutcome,
    NodeOutcomes,
    ToolServer,
)
from boba.toolkit.facade import tool
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import (
    ErrorResult,
    ExceptionResult,
    MarkdownResult,
    ToolResultBase,
)
from boba.toolrun.call_id import CallFields
from boba.toolrun.hosted import ToolHosting
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.stream_calls import LocalDagService
from boba.toolrun.streams import CallJournals, ToolStream

THREAD = "chat-calls"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


class TestChannelTap:
    """ToolRunLogger обязан подключить приёмники каналов: без обстановки вызова
    канальный запуск не журналирует ни байта."""

    @pytest.mark.anyio
    async def test_channels_tap_is_set_during_the_call(
        self, call_stand: CallStand, tmp_path: Path
    ) -> None:
        contexts = call_stand.contexts
        runs = Runs(contexts)
        store = StreamJournal(DirVault(str(tmp_path / "journal")), reserve_bytes=0)
        journals = CallJournals(store, runs)
        journals.mark_streamable(["tap_probe"])
        ambient = CallAmbient()
        seen: list[Any] = []

        @tool
        async def tap_probe(
            text: Annotated[str, Field(min_length=1, description="Что вернуть")],
        ) -> MarkdownResult:
            """Фиксирует, какие тапы видит тело во время вызова."""
            seen.append(ambient.sinks())
            return MarkdownResult(text=text)

        bridged = ToolHosting().hosted(tap_probe)
        CallFields().attach_all([bridged])
        ToolRunLogger(journals, contexts, ambient).guard_all([bridged])

        with runs.open(call_stand.context("tap-thread")):
            await bridged.call(
                DagNode(key="call-tap-1", tool="tap_probe", args={"text": "ping"})
            )

        if len(seen) != 1:
            raise AssertionError(seen)

        stream = seen[0]
        if not isinstance(stream, ToolStream):
            raise AssertionError(
                f"the body sees the call journal in the tap: {stream!r}"
            )
        if ambient.sinks() is not None:
            raise AssertionError(
                "the call journal must leave the ambient after the call"
            )
        if stream.probe(ToolChannel.STDOUT).note != "finished":
            raise AssertionError(stream.probe(ToolChannel.STDOUT).note)


class LoopTurn(FakeTurn):
    """Владелец хода, запоминающий event loop, в котором ему сообщили о
    начале вызова."""

    def __init__(self) -> None:
        super().__init__()
        self.loops: list[int] = []

    async def tool_started(
        self,
        tool_call_id: str,
        name: str,
        args: Mapping[str, Any],
        media: Mapping[str, str] | None,
    ) -> None:
        self.loops.append(id(asyncio.get_running_loop()))
        await super().tool_started(tool_call_id, name, args, media)


class CallsStand:
    """Порт своих инструментов чата, чьи вызовы идут путём ChatCalls, и
    идущий ход с владельцем turn. Создаётся тестом на каждый сценарий."""

    def __init__(self, call_stand: CallStand, turn: FakeTurn) -> None:
        self._calls = call_stand
        self.turn = turn
        self.runs = Runs(call_stand.contexts)
        self.sent = SentConnections()
        contexts = call_stand.contexts
        mount = ChatMount(contexts, self.runs)

        @tool
        def sync_echo(
            text: Annotated[str, Field(min_length=1, description="Что вернуть")],
        ) -> MarkdownResult:
            """Возвращает текст."""
            return MarkdownResult(text=text)

        self.service: ToolServer = LocalDagService(
            ToolHosting().toolset([sync_echo]),
            StandRefs.STREAM_CONFIG,
            (),
            StreamSpecs({}),
            CallAmbient(),
            ChatCalls(
                contexts,
                self.runs,
                self.sent,
                mount,
                RemoteJournals(FakeThreadMessages()),
            ),
        )

    async def called(self, node: DagNode) -> NodeOutcome:
        """Вызов узла внутри идущего хода, как его шлёт граф."""
        with self.runs.open(self._calls.context(THREAD), self.turn):
            return await self.service.call(node)

    async def called_outside(self, node: DagNode) -> NodeOutcome:
        """Тот же вызов без идущего хода: владельца с лентой у запуска нет."""
        with self._calls.applied(self._calls.context(THREAD)):
            return await self.service.call(node)


class TestChatCalls:
    """Путь вызова узла чата: владелец хода узнаёт начало вызова и его итог
    напрямую от ChatCalls — по ним лента рисует шаг."""

    @pytest.mark.anyio
    async def test_step_events_run_in_the_caller_loop(
        self, call_stand: CallStand
    ) -> None:
        """Sync-тело уходит в поток, а события шага остаются в loop хода:
        в чужом loop шаги ленты не доходят до пула postgres."""
        turn = LoopTurn()
        stand = CallsStand(call_stand, turn)
        node = DagNode(key="call-loop-1", tool="sync_echo", args={"text": "hi"})

        outcome = await stand.called(node)

        if outcome.content != "hi":
            raise AssertionError(f"тело исполнилось: {outcome.content!r}")

        if turn.loops != [id(asyncio.get_running_loop())]:
            raise AssertionError(f"событие шага шло в чужом loop: {turn.loops}")

    @pytest.mark.anyio
    async def test_start_and_end_carry_the_call(self, call_stand: CallStand) -> None:
        stand = CallsStand(call_stand, FakeTurn())
        args = {"text": "hi", ToolIntent.NAME: "показываю эхо"}
        node = DagNode(key="call-events-1", tool="sync_echo", args=args)

        outcome = await stand.called(node)

        if stand.turn.started != [("call-events-1", "sync_echo", args)]:
            raise AssertionError(f"начало вызова: {stand.turn.started}")

        if len(stand.turn.finished) != 1 or stand.turn.failed:
            raise AssertionError(f"конец вызова один: {stand.turn.finished}")

        call_id, result = stand.turn.finished[0]
        if call_id != "call-events-1":
            raise AssertionError(f"итог отвечает своему вызову: {call_id!r}")
        if result != MarkdownResult(text="hi"):
            raise AssertionError(f"итог шага — результат тела: {result!r}")
        if outcome.artifact is not result:
            raise AssertionError("итог узла несёт тот же результат, что и шаг")
        if outcome.errored or outcome.key != "call-events-1":
            raise AssertionError(f"итог узла — удача своего вызова: {outcome!r}")

    @pytest.mark.anyio
    async def test_refused_arguments_close_the_step_as_failed(
        self, call_stand: CallStand
    ) -> None:
        stand = CallsStand(call_stand, FakeTurn())
        node = DagNode(key="call-events-2", tool="sync_echo", args={"text": ""})

        outcome = await stand.called(node)

        if not outcome.errored:
            raise AssertionError(f"отказ схемы — ошибка вызова: {outcome!r}")
        if not isinstance(outcome.artifact, ExceptionResult):
            raise AssertionError(f"отказ упакован результатом: {outcome.artifact!r}")

        if len(stand.turn.started) != 1 or stand.turn.finished:
            raise AssertionError(
                f"вызов начат и удачей не кончен: {stand.turn.finished}"
            )
        expected = [("call-events-2", outcome.artifact.chat_view().markdown)]
        if stand.turn.failed != expected:
            raise AssertionError(f"шаг закрыт отказом вызова: {stand.turn.failed}")
        if "text" not in stand.turn.failed[0][1]:
            raise AssertionError(f"отказ называет аргумент: {stand.turn.failed}")

    @pytest.mark.anyio
    async def test_sealed_argument_is_shown_as_its_name(
        self, call_stand: CallStand
    ) -> None:
        """Шаг показывает имя соединения, а не запечатанное значение,
        с которым вызов ушёл исполнителю."""
        stand = CallsStand(call_stand, FakeTurn())
        stand.sent.remember("sealed-blob", "main")
        node = DagNode(
            key="call-seal-1", tool="sync_echo", args={"text": "sealed-blob"}
        )

        outcome = await stand.called(node)

        if outcome.content != "sealed-blob":
            raise AssertionError(f"тело получило запечатанное: {outcome.content!r}")
        shown = stand.turn.started[0][2]
        if shown != {"text": "main"}:
            raise AssertionError(f"шаг показывает имя: {shown}")

    @pytest.mark.anyio
    async def test_call_outside_a_turn_runs_without_steps(
        self, call_stand: CallStand
    ) -> None:
        stand = CallsStand(call_stand, FakeTurn())
        node = DagNode(key="call-quiet-1", tool="sync_echo", args={"text": "hi"})

        outcome = await stand.called_outside(node)

        if outcome.content != "hi" or outcome.errored:
            raise AssertionError(f"тело исполнилось: {outcome!r}")
        if stand.turn.started or stand.turn.finished or stand.turn.failed:
            raise AssertionError(f"без хода шагов нет: {stand.turn.started}")


class TestToolMessages:
    """Итог вызова сообщением инструмента и обратно: статус, идентификатор,
    результат, адрес журнала."""

    @staticmethod
    def _outcome(
        result: ToolResultBase, errored: bool, journal: JournalAddress | None
    ) -> NodeOutcome:
        call = DagNode(key="call-1", tool="probe")
        outcome = NodeOutcomes().of(call, result, errored)

        return outcome.model_copy(update={"journal": journal})

    def test_outcome_becomes_a_success_message(self) -> None:
        outcome = self._outcome(MarkdownResult(text="done"), False, None)

        message = LangchainMessages().tool_message(outcome)

        if message.status != "success" or message.content != "done":
            raise AssertionError(f"удачный итог: {message!r}")
        if message.tool_call_id != "call-1" or message.name != "probe":
            raise AssertionError(f"сообщение отвечает вызову: {message!r}")
        if message.artifact != MarkdownResult(text="done"):
            raise AssertionError(f"артефакт итога: {message.artifact!r}")
        if LangchainMessages.JOURNAL_KEY in message.response_metadata:
            raise AssertionError("у своего вызова адреса журнала нет")

    def test_errored_outcome_becomes_an_error_message(self) -> None:
        outcome = self._outcome(MarkdownResult(text="done"), True, None)

        message = LangchainMessages().tool_message(outcome)

        if message.status != "error":
            raise AssertionError(f"ошибка вызова — статус error: {message!r}")

    def test_journal_address_travels_in_the_message(self) -> None:
        address = JournalAddress(server="boba", run="run-1", call="api-1")
        outcome = self._outcome(MarkdownResult(text="done"), False, address)

        message = LangchainMessages().tool_message(outcome)

        if message.response_metadata[LangchainMessages.JOURNAL_KEY] != {
            "server": "boba",
            "run": "run-1",
            "call": "api-1",
        }:
            raise AssertionError(f"формат адреса в истории: {message!r}")

    def test_message_reads_back_as_the_same_outcome(self) -> None:
        address = JournalAddress(server="boba", run="run-1", call="api-1")
        failure = ErrorResult(message="no such table", error_kind="tool_error")
        for outcome in (
            self._outcome(MarkdownResult(text="done"), False, address),
            self._outcome(failure, True, None),
        ):
            messages = LangchainMessages()

            read = messages.outcome_of(messages.tool_message(outcome))

            if read != outcome:
                raise AssertionError(f"обратный перевод теряет итог: {read!r}")

    def test_stored_message_reads_back_from_its_serialized_artifact(self) -> None:
        """История хранит артефакт сериализованным dict'ом (langgraph)."""
        outcome = self._outcome(MarkdownResult(text="done"), False, None)
        messages = LangchainMessages()
        message = messages.tool_message(outcome)
        message.artifact = outcome.artifact.model_dump(mode="json")

        read = messages.outcome_of(message)

        if read != outcome:
            raise AssertionError(f"итог из сохранённого сообщения: {read!r}")

    def test_old_message_without_a_result_has_no_outcome(self) -> None:
        messages = LangchainMessages()
        plain = ToolMessage(content="raw text", tool_call_id="call-1")
        if messages.outcome_of(plain) is not None:
            raise AssertionError("запись без результата семейства итога не даёт")

        failed = ToolMessage(content="boom", tool_call_id="call-1", status="error")
        read = messages.outcome_of(failed)
        if read is None or not read.errored:
            raise AssertionError(f"ошибка без артефакта — отказ вызова: {read!r}")
        if (
            not isinstance(read.artifact, ErrorResult)
            or read.artifact.message != "boom"
        ):
            raise AssertionError(f"отказ собран из текста сообщения: {read!r}")
