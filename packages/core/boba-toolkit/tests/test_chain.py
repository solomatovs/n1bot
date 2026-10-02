"""Стыковка цепочек: правила совместимости деклараций портов."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel

from boba.toolkit.chain import (
    ChainCheck,
    ChainMismatchError,
    StreamInput,
    StreamNode,
    StreamOutput,
    StreamPlan,
    StreamPlanError,
)
from boba.toolkit.ports import (
    Inbound,
    Outbound,
    PortDecl,
    RawInbound,
    RawOutbound,
    StreamSpec,
)


class ChunkHead(BaseModel):
    """Заголовок порции теста."""

    kind: Literal["chunk"] = "chunk"


class DoneHead(BaseModel):
    """Заголовок конца потока теста."""

    kind: Literal["done"] = "done"


def _port(annotation: object) -> PortDecl:
    """Декларация единственного порта схемы с полем annotation."""
    schema = type(
        "Schema",
        (BaseModel,),
        {
            "model_config": {"arbitrary_types_allowed": True},
            "__annotations__": {"port": annotation},
        },
    )

    return StreamSpec.of_schema(schema).port("port")


class TestChainCheck:
    def test_matching_kinds_pass(self) -> None:
        source = _port(Annotated[Outbound[ChunkHead | DoneHead], None])
        sink = _port(Annotated[Inbound[ChunkHead | DoneHead], None])

        ChainCheck.ensure(source, sink)

    def test_raw_to_raw_passes(self) -> None:
        source = _port(Annotated[RawOutbound, None])
        sink = _port(Annotated[RawInbound, None])

        ChainCheck.ensure(source, sink)

    def test_port_list_is_checked_by_its_element(self) -> None:
        source = _port(Annotated[Outbound[ChunkHead], None])
        sink = _port(Annotated[Sequence[Inbound[ChunkHead | DoneHead]], None])

        ChainCheck.ensure(source, sink)

    def test_framed_source_cannot_feed_raw_sink(self) -> None:
        """Кадровый поток в сырой вход — рамки кадров попали бы в данные."""
        source = _port(Annotated[Outbound[ChunkHead], None])
        sink = _port(Annotated[RawInbound, None])

        with pytest.raises(ChainMismatchError, match="do not mix"):
            ChainCheck.ensure(source, sink)

    def test_raw_source_cannot_feed_framed_sink(self) -> None:
        source = _port(Annotated[RawOutbound, None])
        sink = _port(Annotated[Inbound[ChunkHead], None])

        with pytest.raises(ChainMismatchError, match="do not mix"):
            ChainCheck.ensure(source, sink)

    def test_missing_kind_is_named_in_the_error(self) -> None:
        source = _port(Annotated[Outbound[ChunkHead | DoneHead], None])
        sink = _port(Annotated[Inbound[ChunkHead], None])

        with pytest.raises(ChainMismatchError, match="done"):
            ChainCheck.ensure(source, sink)

    def test_inbound_source_is_refused(self) -> None:
        source = _port(Annotated[Inbound[ChunkHead], None])
        sink = _port(Annotated[RawInbound, None])

        with pytest.raises(ChainMismatchError, match="written by an outbound"):
            ChainCheck.ensure(source, sink)

    def test_outbound_sink_is_refused(self) -> None:
        source = _port(Annotated[RawOutbound, None])
        sink = _port(Annotated[RawOutbound, None])

        with pytest.raises(ChainMismatchError, match="read by an inbound"):
            ChainCheck.ensure(source, sink)


class _Ports(BaseModel):
    """Подпись трансформа теста: вход, список входов и выход."""

    model_config = {"arbitrary_types_allowed": True}

    feed: Annotated[Inbound[ChunkHead | DoneHead], None]
    extra: Annotated[Sequence[Inbound[ChunkHead | DoneHead]], None]
    out: Annotated[Outbound[ChunkHead | DoneHead], None]


class _Source(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    out: Annotated[Outbound[ChunkHead | DoneHead], None]


class _DoneSource(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    out: Annotated[Outbound[DoneHead], None]


class _ChunkSink(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    feed: Annotated[Inbound[ChunkHead], None]


SOURCE = StreamSpec.of_schema(_Source)
TRANSFORM = StreamSpec.of_schema(_Ports)


def _node(
    key: str,
    spec: StreamSpec,
    output: str | None,
    *inputs: tuple[str, str],
) -> StreamNode:
    bound: list[StreamInput] = []
    for port, channel in inputs:
        bound.append(StreamInput(port=port, channel=channel))

    outputs: tuple[StreamOutput, ...] = ()
    if output is not None:
        declared = spec.outbound()
        port_name = declared[0].name if declared else "out"
        outputs = (StreamOutput(port=port_name, channel=output),)

    return StreamNode(
        key=key, tool=f"t_{key}", spec=spec, outputs=outputs, inputs=tuple(bound)
    )


class TestStreamPlan:
    def test_inputs_follow_the_signature_order(self) -> None:
        """Входы вызова раскладываются по порядку портов подписи, внутри
        порта-списка — в порядке модели: так их разложит ToolCommand."""
        plan = StreamPlan(
            [
                _node("a", SOURCE, "ca"),
                _node("b", SOURCE, "cb"),
                _node("c", SOURCE, "cc"),
                _node(
                    "t",
                    TRANSFORM,
                    None,
                    ("extra", "cc"),
                    ("feed", "ca"),
                    ("extra", "cb"),
                ),
            ]
        )

        order: list[tuple[str, str]] = []
        for bound in plan.inputs_of("t"):
            order.append((bound.port, bound.channel))

        assert order == [("feed", "ca"), ("extra", "cc"), ("extra", "cb")]
        assert plan.route("cb").readers[0].index == 2

    def test_fan_out_and_diamond_are_valid(self) -> None:
        plan = StreamPlan(
            [
                _node("src", SOURCE, "s"),
                _node("l", TRANSFORM, "l", ("feed", "s")),
                _node("r", TRANSFORM, "r", ("feed", "s")),
                _node("j", TRANSFORM, None, ("feed", "l"), ("extra", "r")),
            ]
        )

        assert len(plan.route("s").readers) == 2

    def test_two_writers_are_refused(self) -> None:
        with pytest.raises(StreamPlanError, match="two writers"):
            StreamPlan([_node("a", SOURCE, "x"), _node("b", SOURCE, "x")])

    def test_channel_without_readers_is_refused(self) -> None:
        with pytest.raises(StreamPlanError, match="has no readers"):
            StreamPlan([_node("a", SOURCE, "x")])

    def test_unknown_channel_is_refused(self) -> None:
        with pytest.raises(StreamPlanError, match="which no node writes"):
            StreamPlan([_node("t", TRANSFORM, None, ("feed", "ghost"))])

    def test_cycle_is_refused(self) -> None:
        with pytest.raises(StreamPlanError, match=r"cycle: t_a .* -> t_b .* -> t_a"):
            StreamPlan(
                [
                    _node("a", TRANSFORM, "x", ("feed", "y")),
                    _node("b", TRANSFORM, "y", ("feed", "x")),
                ]
            )

    def test_single_port_takes_one_channel(self) -> None:
        with pytest.raises(StreamPlanError, match="takes at most one channel, got 2"):
            StreamPlan(
                [
                    _node("a", SOURCE, "x"),
                    _node("b", SOURCE, "y"),
                    _node("t", TRANSFORM, None, ("feed", "x"), ("feed", "y")),
                ]
            )

    def test_unbound_single_port_reads_nothing(self) -> None:
        plan = StreamPlan(
            [_node("a", SOURCE, "x"), _node("t", TRANSFORM, None, ("extra", "x"))]
        )

        assert plan.inputs_of("t") == (StreamInput(port="extra", channel="x"),)

    def test_undeclared_port_is_refused(self) -> None:
        with pytest.raises(StreamPlanError, match="its inbound ports are"):
            StreamPlan(
                [_node("a", SOURCE, "x"), _node("t", TRANSFORM, None, ("nope", "x"))]
            )

    def test_output_without_outbound_port_is_refused(self) -> None:
        sink = StreamSpec.of_schema(_ChunkSink)
        with pytest.raises(StreamPlanError, match="outbound ports are"):
            StreamPlan([_node("s", sink, "x")])

    def test_kind_mismatch_names_the_channel(self) -> None:
        with pytest.raises(StreamPlanError, match=r"channel 'x' from t_a .* into t_s"):
            StreamPlan(
                [
                    _node("a", StreamSpec.of_schema(_DoneSource), "x"),
                    _node("s", StreamSpec.of_schema(_ChunkSink), None, ("feed", "x")),
                ]
            )
