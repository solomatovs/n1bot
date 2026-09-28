"""Стыковка цепочек: правила совместимости деклараций портов."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel

from boba.toolkit.chain import ChainCheck, ChainMismatchError
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
