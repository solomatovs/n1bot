"""Контракты reload: атомарная публикация, отмена и освобождение поколений."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from boba.mcp_server.catalog import EndpointCatalog, McpEndpoint
from boba.mcp_server.endpoints import EndpointStoreError, McpEndpointStore
from boba.mcp_server.reload import McpReloadError, McpShutdownError, ReloadableMcp
from boba.mcp_server.server import McpEndpoints

pytestmark = pytest.mark.anyio


class ControlledStore(McpEndpointStore):
    """Источник снимков с барьером вместо БД для воспроизводимых гонок reload."""

    def __init__(self) -> None:
        self.records: Sequence[McpEndpoint] = (
            McpEndpoint(id=uuid4(), prefix_url="/old", title="old"),
        )
        self.failure: Exception | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.reads = 0
        self.running = 0
        self.peak = 0

    async def endpoint_list(self) -> Sequence[McpEndpoint]:
        self.reads += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.entered.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure

            return self.records
        finally:
            self.running -= 1


class ControlledEndpoints(McpEndpoints):
    """ASGI-поколение с управляемыми отказами startup, shutdown и запросов."""

    def __init__(self, catalog: EndpointCatalog, factory: EndpointFactory) -> None:
        self._catalog = catalog
        self._factory = factory
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.closing = asyncio.Event()
        self.finish_close = asyncio.Event()
        self.finish_close.set()
        self.requested = asyncio.Event()
        self.finish_request = asyncio.Event()
        self.stopped = 0
        self._app = Starlette(routes=[Route("/", self.reply)], lifespan=self.lifespan)

    def paths(self) -> Sequence[str]:
        return tuple(endpoint.prefix_url for endpoint in self._catalog.endpoints())

    def app(self) -> Starlette:
        return self._app

    def end_subscriptions(self) -> None:
        self.stopped += 1

    async def reply(self, request: Request) -> PlainTextResponse:
        self.requested.set()
        if request.query_params.get("wait"):
            await self.finish_request.wait()

        return PlainTextResponse(",".join(self.paths()))

    @asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncGenerator[None, None]:
        self.entered.set()
        self._factory.preparing.set()
        try:
            await self._factory.start.wait()
            if self._factory.fail_start:
                raise RuntimeError("injected startup failure")

            yield
        finally:
            self.closing.set()
            await self.finish_close.wait()
            self.closed.set()
            if self._factory.fail_close:
                raise RuntimeError("injected cleanup failure")


class EndpointFactory:
    """Создаёт подконтрольные поколения; тест наблюдает только их жизненный цикл."""

    def __init__(self) -> None:
        self.built: list[ControlledEndpoints] = []
        self.start = asyncio.Event()
        self.start.set()
        self.preparing = asyncio.Event()
        self.fail_start = False
        self.fail_close = False

    def build(self, catalog: EndpointCatalog) -> McpEndpoints:
        endpoints = ControlledEndpoints(catalog, self)
        self.built.append(endpoints)
        return endpoints


class ReloadCase:
    """Сборка reload с управляемыми границами I/O и настоящим ASGI-dispatch."""

    def __init__(self) -> None:
        self.store = ControlledStore()
        self.cache = EndpointCatalog(())
        self.factory = EndpointFactory()
        self.reload = ReloadableMcp(self.store, self.cache, self.factory.build)
        self.app = self.reload.app()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    def change(self) -> None:
        self.store.records = (McpEndpoint(id=uuid4(), prefix_url="/new", title="new"),)


async def test_startup_and_reload_share_reader_and_requests_never_read_store() -> None:
    case = ReloadCase()
    assert case.reload.app() is case.app
    async with case.client:
        assert (await case.client.get("/")).status_code == 503
        async with case.app.router.lifespan_context(case.app):
            assert case.store.reads == 1
            for _ in range(10):
                assert (await case.client.get("/")).text == "/old"

            assert case.store.reads == 1
            case.change()
            await case.reload.reload()
            assert (await case.client.get("/")).text == "/new"
            assert case.store.reads == 2

    for generation in case.factory.built:
        assert generation.closed.is_set()


@pytest.mark.parametrize("failure", ["read", "duplicate", "startup"])
async def test_failed_reload_preserves_cache_routes_and_can_be_retried(
    failure: str,
) -> None:
    case = ReloadCase()
    async with case.client, case.app.router.lifespan_context(case.app):
        old = case.cache.endpoints()
        case.change()
        if failure == "read":
            case.store.failure = EndpointStoreError("injected SQL failure")
        elif failure == "duplicate":
            case.store.records = (*case.store.records, *case.store.records)
        else:
            case.factory.fail_start = True

        with pytest.raises(McpReloadError) as caught:
            await case.reload.reload()

        assert caught.value.__cause__ is not None
        assert case.cache.endpoints() == old
        assert (await case.client.get("/")).text == "/old"
        assert not case.factory.built[0].closed.is_set()
        case.store.failure = None
        case.factory.fail_start = False
        case.change()
        await case.reload.reload()
        assert (await case.client.get("/")).text == "/new"


@pytest.mark.parametrize("phase", ["read", "startup"])
async def test_cancelled_reload_cleans_candidate_and_preserves_old_generation(
    phase: str,
) -> None:
    case = ReloadCase()
    async with case.client, case.app.router.lifespan_context(case.app):
        case.change()
        if phase == "read":
            case.store.entered.clear()
            case.store.release.clear()
            entered = case.store.entered
        else:
            case.factory.preparing.clear()
            case.factory.start.clear()
            entered = case.factory.preparing

        pending = asyncio.create_task(case.reload.reload())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            assert (await case.client.get("/")).text == "/old"
            if phase == "startup":
                assert case.factory.built[-1].closed.is_set()
        finally:
            case.store.release.set()
            case.factory.start.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

        await case.reload.reload()
        assert (await case.client.get("/")).text == "/new"


@pytest.mark.parametrize("phase", ["read", "startup"])
async def test_shutdown_during_reload_prevents_publication(phase: str) -> None:
    case = ReloadCase()
    async with case.client, case.app.router.lifespan_context(case.app):
        old = case.cache.endpoints()
        case.change()
        if phase == "read":
            case.store.entered.clear()
            case.store.release.clear()
            entered, release = case.store.entered, case.store.release
        else:
            case.factory.preparing.clear()
            case.factory.start.clear()
            entered, release = case.factory.preparing, case.factory.start

        pending = asyncio.create_task(case.reload.reload())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            case.reload.begin_shutdown()
            release.set()
            with pytest.raises(McpReloadError, match="stopping"):
                await asyncio.wait_for(pending, 2)

            assert case.cache.endpoints() == old
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    with pytest.raises(McpReloadError, match="stopping"):
        await case.reload.reload()

    for generation in case.factory.built:
        assert generation.closed.is_set()


async def test_old_request_drains_while_new_requests_use_new_snapshot() -> None:
    case = ReloadCase()
    async with case.client, case.app.router.lifespan_context(case.app):
        old = case.factory.built[0]
        pending = asyncio.create_task(case.client.get("/?wait=1"))
        try:
            await asyncio.wait_for(old.requested.wait(), 2)
            case.change()
            await case.reload.reload()
            assert not old.closed.is_set()
            assert not pending.done()
            assert (await case.client.get("/")).text == "/new"
            old.finish_request.set()
            assert (await asyncio.wait_for(pending, 2)).text == "/old"
            await asyncio.wait_for(old.closed.wait(), 2)
        finally:
            old.finish_request.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_concurrent_reloads_serialize_reads_and_prepare() -> None:
    case = ReloadCase()
    async with case.client, case.app.router.lifespan_context(case.app):
        case.store.entered.clear()
        case.store.release.clear()
        tasks = [asyncio.create_task(case.reload.reload()) for _ in range(12)]
        try:
            await asyncio.wait_for(case.store.entered.wait(), 2)
            assert case.store.running == 1
            case.store.release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
            assert case.store.peak == 1
            assert case.store.reads == 13
        finally:
            case.store.release.set()
            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)


async def test_cleanup_failure_is_not_lost_on_shutdown() -> None:
    case = ReloadCase()
    async with case.client:
        with pytest.raises(McpShutdownError, match="injected cleanup failure"):
            async with case.app.router.lifespan_context(case.app):
                case.factory.fail_close = True

    assert case.factory.built[0].closed.is_set()


async def test_failed_generation_does_not_skip_other_generations_cleanup() -> None:
    case = ReloadCase()
    context = case.app.router.lifespan_context(case.app)
    await context.__aenter__()
    old = case.factory.built[0]
    old.finish_close.clear()
    await case.reload.reload()
    current = case.factory.built[1]
    current.finish_close.clear()
    case.factory.fail_close = True
    old.finish_close.set()
    await asyncio.wait_for(old.closed.wait(), 2)
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(current.closing.wait(), 2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.05)

        current.finish_close.set()
        with pytest.raises(McpShutdownError) as caught:
            await asyncio.wait_for(closing, 2)

        group = caught.value.__cause__
        assert isinstance(group, ExceptionGroup)
        assert len(group.exceptions) == 2
        assert current.closed.is_set()
    finally:
        current.finish_close.set()
        old.finish_close.set()
        await asyncio.gather(closing, return_exceptions=True)
        await case.client.aclose()


@pytest.mark.parametrize("failure", ["read", "duplicate", "startup"])
async def test_initial_load_failure_escapes_lifespan_without_publishing(
    failure: str,
) -> None:
    case = ReloadCase()
    if failure == "read":
        case.store.failure = EndpointStoreError("injected SQL failure")
    elif failure == "duplicate":
        case.store.records = (*case.store.records, *case.store.records)
    else:
        case.factory.fail_start = True

    async with case.client:
        with pytest.raises(McpReloadError) as caught:
            async with case.app.router.lifespan_context(case.app):
                pytest.fail("startup accepted an invalid catalog")

        assert caught.value.__cause__ is not None
        assert case.store.reads == 1
        assert case.cache.endpoints() == ()
        assert (await case.client.get("/")).status_code == 503
        for generation in case.factory.built:
            assert generation.closed.is_set()

        # Неуспешный lifespan закрыт; восстановление startup требует нового владельца.
        with pytest.raises(McpReloadError, match="stopping"):
            await case.reload.reload()
