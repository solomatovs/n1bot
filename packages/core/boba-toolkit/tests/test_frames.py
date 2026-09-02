"""Кадры вызова: кодек, прямой вход в пайп тела и приёмник кадров тела."""

from __future__ import annotations

import os
import threading
import time

import pytest
from pydantic import BaseModel

from boba.toolkit.frames import (
    CallInbox,
    FrameCodec,
    FrameLimit,
    FrameProtocolError,
    ToolFrame,
)
from boba.toolkit.launcher import LauncherError
from boba.toolkit.pump import CallInput, FrameInput


class Head(BaseModel):
    """Заголовок прикладного кадра теста."""

    kind: str = "chunk"
    seq: int


def _codec() -> FrameCodec:
    return FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)


class TestCodec:
    def test_roundtrip_keeps_header_and_body(self) -> None:
        frame = ToolFrame.of(Head(seq=7), b"\x00\x01\x02")

        decoded = _codec().feed(_codec().encode(frame))

        assert len(decoded) == 1
        assert decoded[0].body == b"\x00\x01\x02"
        assert decoded[0].header_as(Head).seq == 7

    def test_frame_split_across_chunks_is_assembled(self) -> None:
        data = _codec().encode(ToolFrame.of(Head(seq=1), b"payload"))
        codec = _codec()

        collected: list[ToolFrame] = []
        for index in range(len(data)):
            collected.extend(codec.feed(data[index : index + 1]))

        assert len(collected) == 1
        assert collected[0].body == b"payload"

    def test_several_frames_in_one_chunk(self) -> None:
        codec = _codec()
        data = b"".join(
            (
                codec.encode(ToolFrame.of(Head(seq=1), b"a")),
                codec.encode(ToolFrame.of(Head(seq=2), b"b")),
            )
        )

        decoded = _codec().feed(data)

        assert [frame.body for frame in decoded] == [b"a", b"b"]

    def test_body_over_limit_is_refused(self) -> None:
        codec = FrameCodec(FrameLimit.HEADER_BYTES, 4)

        with pytest.raises(FrameProtocolError, match="body"):
            codec.encode(ToolFrame.of(Head(seq=1), b"too long"))

    def test_truncated_frame_is_reported_on_finish(self) -> None:
        data = _codec().encode(ToolFrame.of(Head(seq=1), b"payload"))
        codec = _codec()
        codec.feed(data[:-2])

        with pytest.raises(FrameProtocolError, match="inside a frame"):
            codec.finish()


def _read_all(fd: int) -> bytes:
    """Читает пайп до EOF; зовётся после закрытия пишущего конца."""
    collected = bytearray()

    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return bytes(collected)

        collected.extend(chunk)


class TestCallInput:
    def test_frames_arrive_in_order_and_finish_gives_eof(self) -> None:
        read_fd, write_fd = os.pipe()
        entry = FrameInput(write_fd)

        entry.send(ToolFrame.of(Head(seq=1), b"a"))
        entry.send(ToolFrame.of(Head(seq=2), b"b"))
        entry.finish()

        decoded = _codec().feed(_read_all(read_fd))
        os.close(read_fd)

        assert [frame.header_as(Head).seq for frame in decoded] == [1, 2]

    def test_send_blocks_until_the_reader_catches_up(self) -> None:
        """Полный буфер пайпа держит send — это и есть backpressure."""
        read_fd, write_fd = os.pipe()
        entry = CallInput(write_fd)

        payload = b"\x01" * (4 * 1024 * 1024)
        done = threading.Event()

        def send() -> None:
            entry.send_bytes(payload)
            done.set()

        writer = threading.Thread(target=send, daemon=True)
        writer.start()

        # буфер пайпа кратно меньше полезной нагрузки: запись обязана встать
        assert not done.wait(0.2)

        drained = bytearray()
        while len(drained) < len(payload):
            drained.extend(os.read(read_fd, 65536))

        writer.join(timeout=5)
        os.close(read_fd)

        assert done.is_set()
        assert drained == payload

    def test_broken_pipe_is_quiet_once_then_refused(self) -> None:
        """Смерть читателя: запись молчит (причину объяснит итог вызова),
        следующая попытка — ошибка закрытого входа."""
        read_fd, write_fd = os.pipe()
        entry = CallInput(write_fd)
        os.close(read_fd)

        entry.send_bytes(b"into the void")

        with pytest.raises(LauncherError, match="stopped reading"):
            entry.send_bytes(b"again")

    def test_send_after_finish_is_refused(self) -> None:
        read_fd, write_fd = os.pipe()
        entry = CallInput(write_fd)

        entry.finish()

        with pytest.raises(LauncherError, match="already closed"):
            entry.send_bytes(b"late")

        os.close(read_fd)

    def test_finish_and_abandon_are_idempotent(self) -> None:
        read_fd, write_fd = os.pipe()
        entry = FrameInput(write_fd)

        entry.finish()
        entry.finish()
        entry.abandon()

        assert _read_all(read_fd) == b""
        os.close(read_fd)

    def test_concurrent_writers_do_not_interleave_frames(self) -> None:
        """Записи атомарны: кадры двух потоков не перемешиваются побайтово."""
        read_fd, write_fd = os.pipe()
        entry = FrameInput(write_fd)

        per_writer = 50
        body = b"\x02" * 4096

        def write_many(start: int) -> None:
            for seq in range(start, start + per_writer):
                entry.send(ToolFrame.of(Head(seq=seq), body))

        first = threading.Thread(target=write_many, args=(0,))
        second = threading.Thread(target=write_many, args=(1000,))

        collected = bytearray()
        codec = _codec()
        frames: list[ToolFrame] = []

        first.start()
        second.start()

        deadline = time.monotonic() + 10
        while len(frames) < per_writer * 2 and time.monotonic() < deadline:
            chunk = os.read(read_fd, 65536)
            collected.extend(chunk)
            frames.extend(codec.feed(chunk))

        first.join(timeout=5)
        second.join(timeout=5)
        entry.finish()
        os.close(read_fd)

        assert len(frames) == per_writer * 2
        for frame in frames:
            assert frame.body == body


class TestInbox:
    def test_frames_arrive_before_close(self) -> None:
        inbox = CallInbox()
        codec = _codec()

        def feeder() -> None:
            inbox.feed(codec.encode(ToolFrame.of(Head(seq=1), b"a")))
            inbox.feed(codec.encode(ToolFrame.of(Head(seq=2), b"b")))
            inbox.close()

        thread = threading.Thread(target=feeder)
        thread.start()

        seen = [frame.header_as(Head).seq for frame in inbox.frames()]
        thread.join()

        assert seen == [1, 2]

    def test_broken_stream_raises_on_reader_side(self) -> None:
        inbox = CallInbox()
        inbox.feed(b"\xff\xff\xff\xff")
        inbox.close()

        with pytest.raises(FrameProtocolError):
            list(inbox.frames())
