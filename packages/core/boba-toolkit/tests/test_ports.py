"""Декларативные порты: валидация заголовков на границе, интроспекция
StreamSpec, отказ битых деклараций."""

from __future__ import annotations

import os
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ValidationError

from boba.stand.fake_toolmod import fake_echo, fake_stream
from boba.toolkit.entry import ToolArgv, ToolMain
from boba.toolkit.frames import (
    FrameCodec,
    FrameLimit,
    FrameProtocolError,
    ToolFrame,
    ToolIo,
)
from boba.toolkit.ports import (
    Inbound,
    PortDeclarationError,
    PortDirection,
    RawInbound,
    RawOutbound,
    StreamPorts,
    StreamSpec,
)

STREAM = ToolMain.toolset(fake_stream)[0]
ECHO = ToolMain.toolset(fake_echo)[0]


class ChunkHead(BaseModel):
    """Заголовок порции теста."""

    kind: Literal["chunk"] = "chunk"
    seq: int


class DoneHead(BaseModel):
    """Заголовок конца потока теста."""

    kind: Literal["done"] = "done"
    total: int


class LooseHead(BaseModel):
    """Заголовок без Literal-kind: негодная декларация порта."""

    kind: str = "loose"


def _codec() -> FrameCodec:
    return FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)


def _feed_io(*frames: ToolFrame) -> ToolIo:
    """ToolIo с готовым входом: кадры лежат в пайпе, писатель закрыт."""
    read_fd, write_fd = os.pipe()
    codec = _codec()

    for frame in frames:
        os.write(write_fd, codec.encode(frame))

    os.close(write_fd)

    return ToolIo.on_channels(read_fd, -1)


class TestInbound:
    def test_heads_are_validated_into_models(self) -> None:
        io = _feed_io(
            ToolFrame.of(ChunkHead(seq=1), b"a"),
            ToolFrame.of(DoneHead(total=1)),
        )
        annotation = Inbound[ChunkHead | DoneHead]
        port = StreamPorts.build(annotation, io)

        assert isinstance(port, Inbound)
        received = list(port)

        first = received[0]
        assert isinstance(first.head, ChunkHead)
        assert first.head.seq == 1
        assert first.body == b"a"

        second = received[1]
        assert isinstance(second.head, DoneHead)
        assert second.head.total == 1

    def test_bodies_are_owning_views_without_copies(self) -> None:
        """Тело кадра — memoryview на собственный буфер: живёт после
        итерации и склеивается с bytes явной копией."""
        io = _feed_io(
            ToolFrame.of(ChunkHead(seq=1), b"first"),
            ToolFrame.of(ChunkHead(seq=2), b"second"),
        )
        port = StreamPorts.build(Inbound[ChunkHead], io)

        assert isinstance(port, Inbound)
        kept = list(port)

        assert isinstance(kept[0].body, memoryview)
        assert bytes(kept[0].body) == b"first"
        assert bytes(kept[1].body) == b"second"

    def test_foreign_kind_raises_at_the_reader(self) -> None:
        io = _feed_io(ToolFrame.of(DoneHead(total=9)))
        port = StreamPorts.build(Inbound[ChunkHead], io)

        assert isinstance(port, Inbound)
        with pytest.raises(FrameProtocolError, match="declared port"):
            list(port)


class TestStreamSpec:
    def test_spec_lists_ports_and_kinds(self) -> None:
        spec = StreamSpec.of_schema(ToolArgv.schema_of(STREAM))

        assert spec.streaming()
        assert spec.kinds(PortDirection.INBOUND) == ("chunk", "done")
        assert spec.kinds(PortDirection.OUTBOUND) == ("chunk", "done")

    def test_tool_without_ports_has_empty_spec(self) -> None:
        spec = StreamSpec.of_schema(ToolArgv.schema_of(ECHO))

        assert not spec.streaming()
        assert spec.kinds(PortDirection.INBOUND) == ()

    def test_two_ports_of_one_direction_are_refused(self) -> None:
        class TwoInbound(BaseModel):
            model_config = {"arbitrary_types_allowed": True}

            first: Annotated[Inbound[ChunkHead], None]
            second: Annotated[Inbound[DoneHead], None]

        with pytest.raises(ValidationError, match="duplicate"):
            StreamSpec.of_schema(TwoInbound)

    def test_head_without_literal_kind_is_refused(self) -> None:
        with pytest.raises(PortDeclarationError, match="Literal"):
            StreamPorts.kinds_of(Inbound[LooseHead])

    def test_ports_are_not_argv_and_not_injected(self) -> None:
        schema = ToolArgv.schema_of(STREAM)

        ports = ToolArgv.port_fields(schema)
        injected = ToolArgv.injected_fields(schema)

        assert set(ports) == {"feed", "out"}
        assert set(injected) == {"cfg"}


class TestRawPorts:
    def test_raw_inbound_yields_bytes_verbatim(self) -> None:
        """Истинно сырой вход: байты с провода как есть, без кадров и разбора."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"csv,line,1\ncsv,line,2\n")
        os.close(write_fd)

        io = ToolIo.on_channels(read_fd, -1)
        port = StreamPorts.build(RawInbound, io)

        assert isinstance(port, RawInbound)
        assert b"".join(port) == b"csv,line,1\ncsv,line,2\n"

    def test_raw_outbound_writes_bytes_verbatim(self) -> None:
        """Истинно сырой выход: на проводе ровно те байты, что отдал write."""
        read_fd, write_fd = os.pipe()
        io = ToolIo.on_channels(-1, write_fd)

        port = StreamPorts.build(RawOutbound, io)
        assert isinstance(port, RawOutbound)

        port.write(b"\x01\x02\x03")
        port.write(b"tail")
        os.close(write_fd)

        collected = bytearray()
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break

            collected.extend(chunk)

        os.close(read_fd)

        assert bytes(collected) == b"\x01\x02\x03tail"

    def test_raw_ports_in_spec_have_no_kinds(self) -> None:
        class RelaySchema(BaseModel):
            model_config = {"arbitrary_types_allowed": True}

            feed: Annotated[RawInbound, None]
            out: Annotated[RawOutbound, None]

        spec = StreamSpec.of_schema(RelaySchema)

        assert spec.streaming()
        inbound = spec.ports[0]
        assert inbound.raw
        assert inbound.kinds == ()
