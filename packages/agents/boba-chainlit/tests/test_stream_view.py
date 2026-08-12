"""Вывод инструмента в панели: выбор канала, насос, перемотка и скачивание.

Журнал настоящий: файлы пишет writer-поток песочницы, панель их читает — как
в приложении, только вместо стадии в канал пишет тест. Ключевые инварианты:
канал показа выбирается по контракту узла, живой вызов будит насос из потока
журнала, окна остаются фиксированного размера, а чужой пользователь не читает
ни окно, ни файл.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.step import Step

from boba.chainlit.rendering.canvas import CanvasContent, CanvasKind
from boba.chainlit.rendering.chat_view import (
    ChatSink,
    ChatView,
    RecordingSink,
    StepRole,
)
from boba.chainlit.rendering.stream_view import (
    StageTools,
    StageView,
    StreamNote,
    StreamScreen,
    StreamShowRequest,
    StreamTarget,
    ToolStreams,
    window_stream_action,
)
from boba.sandbox.journal import (
    CallJournal,
    DirVault,
    StreamJournal,
    StreamJournalHub,
)
from boba.sandbox.runner import ToolCallContext
from boba.toolkit.channels import Channel, StreamFormat
from boba.toolkit.workflow import EmptyTrailer, StageContract

THREAD = "33333333-3333-3333-3333-333333333333"
USER = "7"
CALL_ID = "call-stream-1"
TOOL_NAME = "fake_bash"
TEXT_STAGE = "fake_bash"
PLAIN_STAGE = "fake_save"
BINARY_STAGE = "fake_export"

CONTRACTS = {
    TEXT_STAGE: StageContract(out=StreamFormat.CSV, result=EmptyTrailer),
    PLAIN_STAGE: StageContract(out=None, result=EmptyTrailer),
    BINARY_STAGE: StageContract(out=StreamFormat.BYTES, result=EmptyTrailer),
}


def _bin_dirs() -> list[str]:
    """В тестах каталоги берутся из PATH; в проде их задаёт конфиг."""
    dirs: list[str] = []

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.startswith("/"):
            continue

        dirs.append(entry)

    return dirs


@pytest.fixture(autouse=True)
def chainlit_context(tmp_path: Path) -> Iterator[None]:
    """Контекст с thread_id сессии, журнал в каталоге и реестр стадий панели."""
    session = SimpleNamespace(thread_id=THREAD)
    token = context_var.set(cast("ChainlitContext", SimpleNamespace(session=session)))

    StreamJournalHub.configure(
        StreamJournal(DirVault(str(tmp_path / "journal")), reserve_bytes=0)
    )
    StageTools.configure([TOOL_NAME], CONTRACTS)

    yield

    StreamJournalHub.reset()
    StageTools.reset()
    # контекст сбрасывается за собой: иначе сессия утечёт в тесты без неё
    context_var.reset(token)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def context_of(call_id: str = CALL_ID, tool: str = TOOL_NAME) -> ToolCallContext:
    return ToolCallContext(
        user_id=USER, thread_id=THREAD, call_id=call_id, tool=tool
    )


def opened(call_id: str = CALL_ID) -> CallJournal:
    """Журнал вызова, открытый как это делает песочница."""
    return StreamJournalHub.get().open(context_of(call_id))


def written(
    call: CallJournal, stage: str, channel: Channel, data: bytes
) -> None:
    """Канал стадии, записанный целиком: sink закрывается своим EOF."""
    sink = call.sink(stage, channel)
    sink.feed(data)
    sink.close()


def recorded(
    stage: str = TEXT_STAGE,
    channel: Channel = Channel.TOOL_PAYLOAD,
    body: bytes = b"output line\n",
    call_id: str = CALL_ID,
) -> None:
    """Завершённый вызов: файлы дописаны, writer-поток остановлен."""
    call = opened(call_id)
    written(call, stage, channel, body)
    call.close("")


def target_of(
    call_id: str = CALL_ID, user_id: str = USER
) -> StreamTarget | None:
    """Цель показа, как её выбирает действие кнопки: адрес сервер решает сам."""
    request = StreamShowRequest.model_validate({"call_id": call_id})

    return ToolStreams.target(user_id, THREAD, request)


class TestChannelChoice:
    """Канал показа выбирается по объявленному формату продукта узла."""

    def test_text_product_is_shown_from_the_payload_journal(self) -> None:
        recorded(stage=TEXT_STAGE, channel=Channel.TOOL_PAYLOAD, body=b"a,b\n1,2\n")

        target = target_of()

        assert target is not None
        assert target.key.stage == TEXT_STAGE
        assert target.key.channel is Channel.TOOL_PAYLOAD

    def test_stage_without_a_product_is_shown_from_stdout(self) -> None:
        call = opened()
        written(call, PLAIN_STAGE, Channel.TOOL_STDOUT, b"saved\n")
        written(call, PLAIN_STAGE, Channel.TOOL_PAYLOAD, b"")
        call.close("")

        target = target_of()

        assert target is not None
        assert target.key.stage == PLAIN_STAGE
        assert target.key.channel is Channel.TOOL_STDOUT

    def test_unknown_stage_falls_back_to_stdout(self) -> None:
        call = opened()
        written(call, "n1", Channel.TOOL_STDOUT, b"graph node\n")
        written(call, "n1", Channel.TOOL_PAYLOAD, b"payload\n")
        call.close("")

        target = target_of()

        assert target is not None
        assert target.key.channel is Channel.TOOL_STDOUT

    def test_binary_product_is_offered_for_download(self) -> None:
        recorded(stage=BINARY_STAGE, channel=Channel.TOOL_PAYLOAD, body=b"\x00\x01")

        target = target_of()
        assert target is not None
        assert target.view is StageView.DOWNLOAD

        channel = RecordingChannel()
        run(StreamScreen.binary(target.key, channel))

        shown = channel.contents[0]
        assert shown.kind is CanvasKind.NOTICE
        assert shown.note == str(StreamNote.BINARY)
        assert shown.url.endswith(
            f"/stream/{THREAD}/{CALL_ID}/{BINARY_STAGE}/tool_payload"
        )

    def test_call_without_journals_is_explained(self) -> None:
        assert target_of("no-such-call") is None


class RecordingChannel:
    """Канал в тестах: копит снапшоты вместо доставки в панель."""

    def __init__(self) -> None:
        self.contents: list[CanvasContent] = []

    async def push(self, content: CanvasContent) -> None:
        self.contents.append(content)


class TestJournalOutlivesTheTurn:
    """Журнал переживает конец хода: история открывает канал заново."""

    def test_window_after_drop_thread(self) -> None:
        recorded(body="прошлый ход".encode())

        ToolStreams.drop_thread(THREAD)

        target = target_of()
        assert target is not None
        piece = ToolStreams.slice_at(target.key, 0)
        assert piece is not None
        assert piece.text == "прошлый ход"
        assert piece.closed is True

    def test_foreign_user_sees_no_journal(self) -> None:
        recorded(body=b"secret")

        assert target_of(user_id="999") is None

    def test_unsafe_call_id_is_refused(self) -> None:
        key = ToolStreams.key_of(
            USER, THREAD, "../../etc/passwd", TEXT_STAGE, Channel.TOOL_PAYLOAD
        )

        assert key is None


class TestPump:
    """Насос переносит хвост журнала по пробуждениям, не накапливая вывод."""

    CHUNKS = 300
    CHUNK = b"x" * 1024

    def _writer(self, call: CallJournal) -> threading.Thread:
        sink = call.sink(TEXT_STAGE, Channel.TOOL_PAYLOAD)

        def write_all() -> None:
            for index in range(self.CHUNKS):
                sink.feed(f"{index:06d} ".encode() + self.CHUNK)
                time.sleep(0.002)
            sink.close()
            call.close("")

        return threading.Thread(target=write_all)

    async def _pumped(self) -> RecordingChannel:
        """Писатель работает в своём потоке параллельно насосу — как стадия."""
        call = opened()
        writer = self._writer(call)
        channel = RecordingChannel()

        target = target_of()
        assert target is not None

        task = await StreamScreen.show(THREAD, target, channel)
        writer.start()
        await asyncio.get_running_loop().run_in_executor(None, writer.join)
        await asyncio.wait_for(task, timeout=10)

        return channel

    def test_snapshots_stay_within_the_window(self) -> None:
        channel = run(self._pumped())

        assert channel.contents
        for content in channel.contents:
            assert len(content.text.encode()) <= StreamJournal.WINDOW_BYTES

    def test_pushes_are_coalesced(self) -> None:
        channel = run(self._pumped())

        assert len(channel.contents) < self.CHUNKS / 10

    def test_final_push_carries_the_tail_and_position(self) -> None:
        channel = run(self._pumped())

        final = channel.contents[-1]
        assert final.kind is CanvasKind.STREAM
        assert f"{self.CHUNKS - 1:06d}" in final.text
        assert final.stream is not None
        assert final.stream.closed is True
        assert final.stream.size == self.CHUNKS * (len(self.CHUNK) + 7)
        assert final.stream.offset + final.stream.window >= final.stream.size
        assert final.stream.stage == TEXT_STAGE
        assert final.stream.channel is Channel.TOOL_PAYLOAD

    def test_show_replaces_the_previous_pump(self) -> None:
        async def scenario() -> tuple[asyncio.Task[None], asyncio.Task[None]]:
            first = opened("call-a")
            written(first, TEXT_STAGE, Channel.TOOL_PAYLOAD, b"a")
            second = opened("call-b")
            second_sink = second.sink(TEXT_STAGE, Channel.TOOL_PAYLOAD)
            second_sink.feed(b"b")

            first_target = target_of("call-a")
            second_target = target_of("call-b")
            assert first_target is not None
            assert second_target is not None

            channel = RecordingChannel()
            first_task = await StreamScreen.show(THREAD, first_target, channel)
            second_task = await StreamScreen.show(THREAD, second_target, channel)

            second_sink.close()
            second.close("")
            await asyncio.wait_for(second_task, timeout=10)
            first.close("")
            return first_task, second_task

        first_task, second_task = run(scenario())

        assert first_task.cancelled() is True
        assert second_task is not first_task

    def test_leave_stops_the_pump(self) -> None:
        async def scenario() -> asyncio.Task[None]:
            call = opened()
            call.sink(TEXT_STAGE, Channel.TOOL_PAYLOAD).feed(b"live")
            target = target_of()
            assert target is not None

            task = await StreamScreen.show(THREAD, target, RecordingChannel())

            StreamScreen.leave(THREAD)
            await asyncio.gather(task, return_exceptions=True)
            call.close("")
            return task

        task = run(scenario())
        assert task.cancelled() is True


class TestWindowAction:
    """Перемотка ходит по журналу окнами и снимает насос."""

    BODY = ("0123456789" * 20000).encode()
    """200 КБ: больше трёх окон журнала."""

    def _address(self, offset: int) -> dict[str, Any]:
        return {
            "call_id": CALL_ID,
            "stage": TEXT_STAGE,
            "channel": Channel.TOOL_PAYLOAD.value,
            "offset": offset,
        }

    def test_windows_walk_the_journal(self) -> None:
        recorded(body=self.BODY)

        first = run(window_stream_action(USER, THREAD, self._address(0)))
        middle = run(window_stream_action(USER, THREAD, self._address(70000)))

        assert first["stream"]["offset"] == 0
        assert first["stream"]["size"] == len(self.BODY)
        assert middle["stream"]["offset"] == 70000
        assert len(middle["text"].encode()) == first["stream"]["window"]

    def test_window_carries_the_channel_address(self) -> None:
        recorded(body=self.BODY)

        first = run(window_stream_action(USER, THREAD, self._address(0)))

        assert first["stream"]["call_id"] == CALL_ID
        assert first["stream"]["stage"] == TEXT_STAGE
        assert first["stream"]["channel"] == Channel.TOOL_PAYLOAD.value

    def test_offset_beyond_the_file_gives_empty_window(self) -> None:
        recorded(body=self.BODY)

        beyond = run(window_stream_action(USER, THREAD, self._address(10**9)))

        assert beyond["text"] == ""
        assert beyond["stream"]["offset"] == len(self.BODY)

    def test_unknown_call_gives_empty_answer(self) -> None:
        payload = self._address(0)
        payload["call_id"] = "no-such-call"

        answer = run(window_stream_action(USER, THREAD, payload))

        assert answer == {}

    def test_foreign_user_gets_empty_answer(self) -> None:
        recorded(body=self.BODY)

        answer = run(window_stream_action("999", THREAD, self._address(0)))

        assert answer == {}

    def test_window_request_stops_the_pump(self) -> None:
        async def scenario() -> asyncio.Task[None]:
            call = opened()
            call.sink(TEXT_STAGE, Channel.TOOL_PAYLOAD).feed(b"live data")
            target = target_of()
            assert target is not None

            task = await StreamScreen.show(THREAD, target, RecordingChannel())
            await window_stream_action(USER, THREAD, self._address(0))
            await asyncio.gather(task, return_exceptions=True)
            call.close("")
            return task

        task = run(scenario())
        assert task.cancelled() is True


class TestShowAction:
    """Кнопка вывода: живой вызов — насос, завершённый — окно из журнала."""

    def test_recorded_stream_is_shown_from_the_journal(self) -> None:
        recorded(body="сохранённый вывод".encode())

        channel = RecordingChannel()

        async def scenario() -> None:
            target = target_of()
            assert target is not None
            piece = ToolStreams.slice_at(target.key, 0)
            assert piece is not None
            await StreamScreen.recorded(target.key, piece, channel, follow=False)

        run(scenario())

        assert len(channel.contents) == 1
        shown = channel.contents[0]
        assert shown.kind is CanvasKind.STREAM
        assert "сохранённый вывод" in shown.text
        assert shown.note == str(StreamNote.FINISHED)

    def test_live_call_is_marked_running(self) -> None:
        call = opened()
        call.sink(TEXT_STAGE, Channel.TOOL_PAYLOAD).feed(b"in progress")

        channel = RecordingChannel()

        async def scenario() -> None:
            target = target_of()
            assert target is not None
            piece = ToolStreams.slice_at(target.key, 0)
            assert piece is not None
            await StreamScreen.recorded(target.key, piece, channel, follow=False)

        run(scenario())
        call.close("")

        assert channel.contents[0].note == str(StreamNote.RUNNING)

    def test_live_call_of_another_user_is_not_live(self) -> None:
        call = opened()
        call.sink(TEXT_STAGE, Channel.TOOL_PAYLOAD).feed(b"mine")

        assert ToolStreams.live_call(USER, THREAD, CALL_ID) is not None
        assert ToolStreams.live_call("999", THREAD, CALL_ID) is None

        call.close("")

    def test_unknown_stream_is_explained(self) -> None:
        channel = RecordingChannel()

        async def scenario() -> None:
            await StreamScreen.gone("no-such", channel)

        run(scenario())

        assert channel.contents[0].kind is CanvasKind.NOTICE
        assert channel.contents[0].note == str(StreamNote.GONE)


class ElementSink(ChatSink):
    """Live-подобный sink: элементы включены, шаги копятся в память."""

    EMITS_ELEMENTS: ClassVar[bool] = True

    def __init__(self) -> None:
        self.steps: list[Step] = []

    async def put(self, step: Step) -> None:
        self.steps.append(step)


class TestStreamButton:
    """Кнопка вывода живёт на шаге инструмента со стадиями и адресуется вызовом."""

    async def _tool_step(self, sink: ChatSink, name: str) -> Step:
        view = ChatView(THREAD, sink, user_name="tester")
        view.begin_turn("turn-1")
        return await view.tool_started(name, {"command": "ls"}, CALL_ID)

    def test_stage_tool_gets_the_button(self) -> None:
        sink = ElementSink()

        step = run(self._tool_step(sink, TOOL_NAME))

        elements = step.elements or []
        assert len(elements) == 1
        element = elements[0]
        assert element.name == "CanvasStream"
        assert getattr(element, "props", {}).get("call_id") == CALL_ID
        assert element.id == ChatView.derive_id(THREAD, CALL_ID, StepRole.STREAM)

    def test_tool_without_stages_stays_clean(self) -> None:
        step = run(self._tool_step(ElementSink(), "diagram_save"))

        assert not step.elements

    def test_replay_sink_never_emits_the_button(self) -> None:
        step = run(self._tool_step(RecordingSink(), TOOL_NAME))

        assert not step.elements

    def test_replayed_step_dict_matches_live(self) -> None:
        """Кнопка не должна ломать контракт шагов: сравниваются StepDict."""
        live = run(self._tool_step(ElementSink(), TOOL_NAME)).to_dict()
        replay = run(self._tool_step(RecordingSink(), TOOL_NAME)).to_dict()

        assert live["id"] == replay["id"]
        assert live["name"] == replay["name"]
        assert live["parentId"] == replay["parentId"]


class TestStreamDownload:
    """Скачивание журнала канала: тот же StreamedFile, что отдаёт вложения."""

    @staticmethod
    def _app(user_id: str, base_dir: str) -> Any:
        from chainlit.auth import get_current_user
        from chainlit.user import PersistedUser
        from fastapi import FastAPI

        from boba.chainlit.data.upload import StreamServing, UploadPolicy
        from boba.chainlit.domain.keys import StreamUrl
        from boba.chainlit.infra.config import LocalStorageConfig

        # files_dir на серве подменяется корнем тома пользователя; здесь нужен
        # лишь валидный конфиг — LocalStorageConfig требует непустой files_dir
        config = LocalStorageConfig.model_validate(
            {
                "kind": "local",
                "files_dir": base_dir,
                "launcher": {
                    "mount_wait_sec": 1.0,
                    "mount_poll_sec": 0.1,
                    "shutdown_wait_sec": 1.0,
                    "lock_wait_sec": 10.0,
                    "copy_chunk_bytes": 65536,
                },
                "binaries": {"dirs": _bin_dirs()},
            }
        )
        serving = StreamServing(config, UploadPolicy())

        app = FastAPI()
        app.add_api_route(StreamUrl.ROUTE, serving.serve, methods=["GET"])
        user = PersistedUser(
            id=user_id, identifier="tester", createdAt="2024-01-01T00:00:00Z"
        )
        app.dependency_overrides[get_current_user] = lambda: user
        return app

    ROUTE: ClassVar[str] = f"/stream/{THREAD}/{CALL_ID}/{TEXT_STAGE}/tool_payload"

    def test_log_downloads_whole_and_by_range(self, tmp_path: Path) -> None:
        body = "строка вывода\n" * 20
        recorded(body=body.encode())

        from httpx import ASGITransport, AsyncClient

        app = self._app(USER, str(tmp_path))

        async def scenario() -> Any:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                whole = await client.get(self.ROUTE)
                part = await client.get(
                    self.ROUTE, headers={"Range": "bytes=0-9"}
                )
                missing = await client.get(
                    f"/stream/{THREAD}/absent-call/{TEXT_STAGE}/tool_payload"
                )
                unknown = await client.get(
                    f"/stream/{THREAD}/{CALL_ID}/{TEXT_STAGE}/no_such_channel"
                )
            return whole, part, missing, unknown

        whole, part, missing, unknown = run(scenario())

        assert whole.status_code == 200
        assert whole.content == body.encode()
        assert whole.headers["content-length"] == str(len(body.encode()))
        assert "attachment" in whole.headers["content-disposition"]
        assert (
            f"{CALL_ID}.{TEXT_STAGE}.tool_payload.log"
            in whole.headers["content-disposition"]
        )

        assert part.status_code == 206
        assert part.content == body.encode()[:10]

        assert missing.status_code == 404
        assert unknown.status_code == 404

    def test_foreign_user_gets_no_log(self, tmp_path: Path) -> None:
        recorded(body=b"secret output")

        from httpx import ASGITransport, AsyncClient

        app = self._app("999", str(tmp_path))

        async def scenario() -> Any:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                return await client.get(self.ROUTE)

        response = run(scenario())
        assert response.status_code == 404
