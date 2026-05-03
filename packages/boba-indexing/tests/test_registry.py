"""ReaderRegistry / SourceRegistry на (Context)CatalogFactory."""

from __future__ import annotations

from collections.abc import Iterable

from boba.config.app import AppConfig
from boba.indexing import (
    IndexerExtensionContext,
    IndexingContext,
    PipelineId,
    Reader,
    ReaderId,
    ReaderRegistry,
    Section,
    Source,
    SourceFactory,
    SourceId,
    SourceItem,
    SourceRegistry,
)


class _StubReader(Reader):
    def __init__(self, hint: str) -> None:
        self._hint = hint

    def name(self) -> str:
        return f"StubReader({self._hint})"

    def reader_id(self) -> ReaderId:
        return ReaderId(self._hint)

    def accepts(self, item: SourceItem) -> bool:
        return item.content_hint == self._hint

    def convert(
        self, ctx: IndexingContext, value: SourceItem
    ) -> Iterable[Section]:
        del ctx
        yield Section(source_id=value.source_id, text=value.payload.decode())


class _StubSource(Source):
    def __init__(self, sid: str) -> None:
        self._sid = sid

    def name(self) -> str:
        return f"StubSource({self._sid})"

    def source_factory_id(self) -> SourceId:
        return SourceId(self._sid)

    def stream(self, ctx: IndexingContext) -> Iterable[SourceItem]:
        del ctx
        yield from ()

    def list_source_ids(self) -> Iterable[str]:
        return ()


class _StubSourceFactory(SourceFactory):
    def __init__(self, sid: str) -> None:
        self._sid = SourceId(sid)
        self.produce_calls: list[IndexerExtensionContext] = []

    def id(self) -> SourceId:
        return self._sid

    def produce(self, ctx: IndexerExtensionContext) -> Source:
        self.produce_calls.append(ctx)
        return _StubSource(self._sid.to_wire())


def test_reader_registry_builds_dispatcher_in_registration_order():
    reg = ReaderRegistry()
    reg.register_reader(_StubReader("md"))
    reg.register_reader(_StubReader("html"))
    dispatcher = reg.build()

    assert "md" in dispatcher.name()
    assert dispatcher.name().index("md") < dispatcher.name().index("html")


def test_reader_registry_last_wins_on_same_id():
    first = _StubReader("md")
    second = _StubReader("md")
    reg = ReaderRegistry()
    reg.register_reader(first)
    reg.register_reader(second)
    dispatcher = reg.build()

    item = SourceItem(source_id="x", content_hint="md", payload=b"q")
    ctx = IndexingContext(
        pipeline_id=PipelineId(type(item).__name__), collection="c"
    )
    sections = list(dispatcher.stream(ctx, [item]))
    assert len(sections) == 1


def test_source_registry_builds_sources_via_factories():
    reg = SourceRegistry()
    f1 = _StubSourceFactory("ext.fs")
    f2 = _StubSourceFactory("ext.http")
    reg.register_factory(f1)
    reg.register_factory(f2)

    ctx = IndexerExtensionContext(config=AppConfig({}))
    catalog = reg.build(ctx)

    assert set(catalog.keys()) == {SourceId("ext.fs"), SourceId("ext.http")}
    assert isinstance(catalog[SourceId("ext.fs")], _StubSource)
    assert len(f1.produce_calls) == 1
    assert len(f2.produce_calls) == 1
