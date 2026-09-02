"""Стыковка цепочек: правила совместимости деклараций портов."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import BaseModel

from boba.toolkit.chain import ChainCheck, ChainMismatchError
from boba.toolkit.ports import Inbound, Outbound, RawInbound, RawOutbound, StreamSpec


class ChunkHead(BaseModel):
    """Заголовок порции теста."""

    kind: Literal["chunk"] = "chunk"


class DoneHead(BaseModel):
    """Заголовок конца потока теста."""

    kind: Literal["done"] = "done"


def _spec(**fields: object) -> StreamSpec:
    schema = type(
        "Schema",
        (BaseModel,),
        {
            "model_config": {"arbitrary_types_allowed": True},
            "__annotations__": dict(fields),
        },
    )

    return StreamSpec.of_schema(schema)


class TestChainCheck:
    def test_matching_kinds_pass(self) -> None:
        source = _spec(out=Annotated[Outbound[ChunkHead | DoneHead], None])
        sink = _spec(feed=Annotated[Inbound[ChunkHead | DoneHead], None])

        ChainCheck.ensure(source, sink)

    def test_raw_to_raw_passes(self) -> None:
        source = _spec(out=Annotated[RawOutbound, None])
        sink = _spec(feed=Annotated[RawInbound, None])

        ChainCheck.ensure(source, sink)

    def test_framed_source_cannot_feed_raw_sink(self) -> None:
        """Кадровый поток в сырой вход — рамки кадров попали бы в данные."""
        source = _spec(out=Annotated[Outbound[ChunkHead], None])
        sink = _spec(feed=Annotated[RawInbound, None])

        with pytest.raises(ChainMismatchError, match="do not mix"):
            ChainCheck.ensure(source, sink)

    def test_raw_source_cannot_feed_framed_sink(self) -> None:
        source = _spec(out=Annotated[RawOutbound, None])
        sink = _spec(feed=Annotated[Inbound[ChunkHead], None])

        with pytest.raises(ChainMismatchError, match="do not mix"):
            ChainCheck.ensure(source, sink)

    def test_missing_kind_is_named_in_the_error(self) -> None:
        source = _spec(out=Annotated[Outbound[ChunkHead | DoneHead], None])
        sink = _spec(feed=Annotated[Inbound[ChunkHead], None])

        with pytest.raises(ChainMismatchError, match="done"):
            ChainCheck.ensure(source, sink)

    def test_source_without_outbound_is_refused(self) -> None:
        source = _spec(feed=Annotated[Inbound[ChunkHead], None])
        sink = _spec(feed=Annotated[RawInbound, None])

        with pytest.raises(ChainMismatchError, match="source declares no"):
            ChainCheck.ensure(source, sink)

    def test_sink_without_inbound_is_refused(self) -> None:
        source = _spec(out=Annotated[RawOutbound, None])
        sink = _spec(out=Annotated[RawOutbound, None])

        with pytest.raises(ChainMismatchError, match="sink declares no"):
            ChainCheck.ensure(source, sink)
