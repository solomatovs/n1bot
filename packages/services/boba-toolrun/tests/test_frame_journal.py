"""Журнал кадровых каналов: FrameHeadsSink пишет заголовки, тела пропускает."""

from __future__ import annotations

from pydantic import BaseModel

from boba.toolkit.frames import FrameCodec, FrameLimit, ToolFrame
from boba.toolkit.stream import Chunk, StreamSink
from boba.toolrun.streams import FrameHeadsSink


class Head(BaseModel):
    """Заголовок кадра теста."""

    kind: str = "chunk"
    seq: int


class Recorder(StreamSink):
    """Приёмник журнала: копит текстовые строки."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def feed(self, data: Chunk) -> None:
        self.lines.append(bytes(data).decode("utf-8", errors="replace"))

    def feed_text(self, text: str) -> None:
        self.lines.append(text)


def _codec() -> FrameCodec:
    return FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)


class TestFrameHeadsSink:
    def test_heads_and_sizes_without_bodies(self) -> None:
        recorder = Recorder()
        sink = FrameHeadsSink(recorder)

        sink.feed(_codec().encode(ToolFrame.of(Head(seq=1), b"\x00" * 3200)))

        journal = "".join(recorder.lines)
        assert '"seq":1' in journal
        assert "+3200b" in journal
        assert "\x00" not in journal

    def test_split_frame_is_assembled_before_the_line(self) -> None:
        recorder = Recorder()
        sink = FrameHeadsSink(recorder)

        data = _codec().encode(ToolFrame.of(Head(seq=7), b"body"))
        sink.feed(data[:5])
        sink.feed(data[5:])

        journal = "".join(recorder.lines)
        assert '"seq":7' in journal

    def test_broken_stream_is_reported_not_silenced(self) -> None:
        recorder = Recorder()
        sink = FrameHeadsSink(recorder)

        sink.feed(b"\xff\xff\xff\xff garbage")

        journal = "".join(recorder.lines)
        assert "frame stream broken" in journal
