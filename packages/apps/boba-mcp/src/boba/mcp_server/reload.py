"""Ручная загрузка каталога MCP и замена опубликованного приложения.

SQL выполняется только при старте и reload. Новый набор маршрутов, OAuth
и серверов сначала подготавливается, затем публикуется вместе с кешем прав.
Запросы старого приложения завершаются перед освобождением его ресурсов.

Ошибки:
McpReloadError — чтение или подготовка каталога не выполнены, рабочий кеш сохранён.
McpShutdownError — освобождение одного или нескольких поколений завершилось ошибкой.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from boba.mcp_server.catalog import EndpointCatalog
from boba.mcp_server.endpoints import McpEndpointStore
from boba.mcp_server.server import McpEndpoints

logger = logging.getLogger(__name__)


class McpReloadError(Exception):
    """Каталог не заменён: подготовка отказала или приложение останавливается."""


class McpShutdownError(Exception):
    """Закрытие поколений завершено, но их lifespan сообщил об ошибке."""


class _PublishedEndpoints:
    """Приложение и его активные запросы; lifespan открывает и закрывает одна задача.

    У FastMCP lifespan содержит task groups: нельзя открыть его в обработчике
    reload, а закрыть в задаче последнего HTTP-запроса.
    """

    def __init__(self, endpoints: McpEndpoints) -> None:
        self.endpoints = endpoints
        self.app = endpoints.app()
        self._requests = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._retired = asyncio.Event()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None]:
        if self._task is None:
            raise McpReloadError(
                "accessing endpoint lifespan: start() has not been called"
            )

        return self._task

    async def start(self) -> None:
        if self._task is not None:
            raise McpReloadError("starting endpoint lifespan: already started")

        self._task = asyncio.create_task(self._run(), name="mcp-endpoints-lifespan")
        try:
            await self._ready
        except BaseException:
            await self._cancel_start()
            raise

    async def _cancel_start(self) -> None:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            return

    async def _run(self) -> None:
        try:
            async with self.app.router.lifespan_context(self.app):
                if not self._ready.done():
                    self._ready.set_result(None)

                await self._retired.wait()
                await self._idle.wait()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)

            raise

    def retire(self) -> None:
        self.endpoints.end_subscriptions()
        self._retired.set()

    async def discard(self) -> None:
        self.retire()
        await self.task

    async def serve(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._requests += 1
        self._idle.clear()
        try:
            await self.app(scope, receive, send)
        finally:
            self._requests -= 1
            if not self._requests:
                self._idle.set()


class _EndpointPublications:
    """Фабрика приложений каталога; ReloadableMcp владеет ею и её результатами."""

    def __init__(self, build: Callable[[EndpointCatalog], McpEndpoints]) -> None:
        self._build = build

    async def prepare(self, snapshot: EndpointCatalog) -> _PublishedEndpoints:
        endpoints = self._build(snapshot)
        candidate = _PublishedEndpoints(endpoints)
        await candidate.start()

        return candidate


class ReloadableMcp:
    """Загружает каталог и публикует HTTP-приложение в цикле событий McpHost.

    Владеет фабрикой приложений, кешем и блокировкой reload. При запуске и
    SIGHUP вызывает одну операцию; подготовленные ресурсы закрывает после запросов.
    """

    def __init__(
        self,
        store: McpEndpointStore,
        cache: EndpointCatalog,
        build: Callable[[EndpointCatalog], McpEndpoints],
    ) -> None:
        self._mcp_store = store
        self._cache = cache
        self._publications = _EndpointPublications(build)
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._current: _PublishedEndpoints | None = None
        self._live: set[_PublishedEndpoints] = set()
        self._ready = asyncio.Event()
        self._closing = False
        self._app = Starlette(
            routes=[Mount("/", app=self._dispatch)], lifespan=self._lifespan
        )

    async def reload(self) -> None:
        """Перечитывает каталог; ошибка до публикации сохраняет рабочую версию."""
        self._check_loop()
        async with self._lock:
            self._ensure_running()
            logger.info("MCP reload: reading endpoint definitions and grants")

            try:
                records = await self._mcp_store.endpoint_list()
                self._ensure_running()

                snapshot = EndpointCatalog(records)
                async with self._replacement(snapshot) as candidate:
                    self._ensure_running()
                    self._publish(snapshot, candidate)
            except Exception as exc:
                msg = f"reloading MCP endpoint catalog failed: {exc}"
                raise McpReloadError(msg) from exc

            paths = list(candidate.endpoints.paths())
            logger.info("MCP reload complete: endpoints=%s", paths)

    @asynccontextmanager
    async def _replacement(
        self, snapshot: EndpointCatalog
    ) -> AsyncGenerator[_PublishedEndpoints, None]:
        """Запускает замену и закрывает её, если владение не перешло в _current."""
        candidate = await self._publications.prepare(snapshot)
        try:
            yield candidate
        finally:
            if candidate is not self._current:
                await candidate.discard()

    def _publish(
        self, snapshot: EndpointCatalog, candidate: _PublishedEndpoints
    ) -> None:
        """Меняет кеш и приложение без await; старые запросы завершаются отдельно."""
        previous = self._current
        self._cache.replace(snapshot)
        self._current = candidate
        self._live.add(candidate)
        candidate.task.add_done_callback(lambda task: self._finished(candidate, task))

        if previous is not None:
            previous.retire()

    def _finished(
        self, published: _PublishedEndpoints, task: asyncio.Task[None]
    ) -> None:
        if task.cancelled():
            self._live.discard(published)
            return

        failure = task.exception()
        if failure is not None:
            # Сохраняем в _live: shutdown обязан получить ошибку освобождения.
            logger.error("MCP endpoint lifespan failed", exc_info=failure)
            return

        self._live.discard(published)

    async def on_reload(self, received: signal.Signals) -> None:
        """Сигнал во время старта ждёт первоначальной загрузки."""
        await self._ready.wait()
        logger.info("%s: reloading MCP", received.name)

        try:
            await self.reload()
        except McpReloadError as exc:
            logger.error("MCP reload rejected; keeping the published catalog: %s", exc)

    def _check_loop(self) -> None:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError as exc:
            msg = "updating MCP catalog: expected its owning event loop"
            raise McpReloadError(msg) from exc

        if current is not self._loop:
            raise McpReloadError("updating MCP catalog: expected its owning event loop")

    def _ensure_running(self) -> None:
        if self._closing:
            raise McpReloadError("updating MCP catalog: the application is stopping")

    def begin_shutdown(self) -> None:
        self._check_loop()
        self._closing = True
        self._ready.set()
        self.end_subscriptions()

    def end_subscriptions(self) -> None:
        self._check_loop()
        for published in tuple(self._live):
            published.endpoints.end_subscriptions()

    def app(self) -> Starlette:
        return self._app

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        current = self._current
        if current is None:
            await self._unavailable(scope, receive, send)
            return

        if current.task.done():
            await self._unavailable(scope, receive, send)
            return

        await current.serve(scope, receive, send)

    async def _unavailable(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"error": "MCP endpoint catalog is unavailable"}, status_code=503
        )
        await response(scope, receive, send)

    @asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncGenerator[None, None]:
        try:
            await self.reload()
            self._ready.set()
            yield
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self.begin_shutdown()

        async with self._lock:
            await self._close_generations()

    async def _close_generations(self) -> None:
        """Дожидается всех поколений и передаёт ошибки их закрытия владельцу."""
        published = tuple(self._live)
        if not published:
            return

        for item in published:
            item.retire()

        tasks = []
        for item in published:
            tasks.append(item.task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        failures: list[BaseException] = []
        for result in results:
            if not isinstance(result, BaseException):
                continue

            failures.append(result)

        if not failures:
            return

        causes = BaseExceptionGroup("MCP lifespans failed", failures)
        details = map(str, failures)
        message = "; ".join(details)
        raise McpShutdownError(
            f"closing MCP endpoint generations failed: {message}"
        ) from causes
