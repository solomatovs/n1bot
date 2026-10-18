"""Журналы вызовов, исполненных MCP-серверами: адреса и окна.

Вызов исполняет сервер, журнал лежит у него. Адрес журнала чат узнаёт у
сервера: пока вызов идёт — ресурсом списка resources/list (ретранслятор
называет его ленте сообщением JournalOpened), у итога — по ссылке
resource_link в `rawOutput` уведомления агента; лента запоминает его здесь и
при живом ходе, и при воспроизведении истории; текст читает окнами
resources/read через клиента MCP, рост канала слушает потоком
subscriptions/listen.

Ошибки:
McpClientError — сервер недоступен или ответил не окном журнала.
WatchProbeError — слежение не сняло состояние канала журнала; опрос
    повторяет сторож панели.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import ClassVar

import mcp_types as mt
from mcp.shared.exceptions import MCPError

from boba.aid.rendering.acp_view import JournalRegistry, ToolMedia
from boba.canvas.canvas import WatchProbe, WatchProbeError, WatchSource
from boba.canvas.journal import StreamSlice
from boba.mcp_client.client import (
    McpCaller,
    McpClientError,
    McpServers,
    McpToolServer,
)
from boba.toolkit.channels import JournalChannel, JournalChannels
from boba.toolkit.dag import JournalAddress
from boba.toolkit.wire import JournalRead

__all__ = ["RemoteJournals", "RemoteStreams"]


logger = logging.getLogger(__name__)


class RemoteJournals(JournalRegistry):
    """Адреса журналов вызовов, исполненных MCP-серверами.

    Объект один на процесс: его создаёт сборка чата. Журнал вызова лежит на
    сервере, который вызов исполнил; его адрес — сервер, запуск и
    идентификатор вызова на сервере — сервер называет, едва журнал открыт,
    и ещё раз в итоге вызова. Лента (UpdateView) запоминает адрес здесь: у
    живого хода — пока вызов идёт, у истории — при её воспроизведении,
    поэтому адреса треда известны, пока открыта его вкладка.
    """

    KEEP: ClassVar[int] = 4096
    """Сколько адресов помнить в памяти; старые забываются первыми."""

    def __init__(self) -> None:
        self._known: OrderedDict[tuple[str, str], JournalAddress] = OrderedDict()

    def remember(self, thread_id: str, call_id: str, address: JournalAddress) -> None:
        """Адрес журнала вызова call_id треда."""
        key = (thread_id, call_id)
        self._known[key] = address
        self._known.move_to_end(key)
        while len(self._known) > self.KEEP:
            self._known.popitem(last=False)

    async def address(self, thread_id: str, call_id: str) -> JournalAddress | None:
        """Адрес журнала вызова; None — его исполнял не сервер с журналом,
        журнал ещё не открыт либо лента треда в этом процессе не
        рисовалась."""
        return self._known.get((thread_id, call_id))


class RemoteStreams:
    """Чтение журналов вызовов MCP-серверов для панели живого вывода.

    Создаётся обработчиком действия панели из реестра журналов, клиента MCP,
    пользователя сессии и знаний ленты о серверах (ToolMedia: адреса
    каналов журнала для подписки): сервер отдаёт журнал только тому, кто
    вызывал.
    Панель зовёт его, когда своего журнала у вызова нет: окно читает
    resources/read канала, адрес журнала даёт реестр RemoteJournals.
    """

    def __init__(
        self,
        journals: RemoteJournals,
        servers: McpServers,
        caller: McpCaller,
        media: ToolMedia,
    ) -> None:
        self._journals = journals
        self._servers = servers
        self._caller = caller
        self._media = media

    async def slice_at(
        self, thread_id: str, call_id: str, channel: JournalChannel, offset: int
    ) -> StreamSlice | None:
        address = await self._journals.address(thread_id, call_id)
        if address is None:
            return None

        request = JournalRead(
            run=address.run, node=address.call, channel=channel, offset=offset
        )

        return await self._servers.journal(address.server, self._caller, request)

    async def slice_before(
        self, thread_id: str, call_id: str, channel: JournalChannel, end: int
    ) -> StreamSlice | None:
        address = await self._journals.address(thread_id, call_id)
        if address is None:
            return None

        request = JournalRead(
            run=address.run, node=address.call, channel=channel, before=end
        )

        return await self._servers.journal(address.server, self._caller, request)

    async def channels(
        self, thread_id: str, call_id: str
    ) -> tuple[JournalChannel, ...]:
        """Видимые каналы журнала вызова: те, что сервер отдаёт окном."""
        readable: list[JournalChannel] = []
        for channel in JournalChannels.VISIBLE:
            piece = await self.slice_at(thread_id, call_id, channel, 0)
            if piece is None:
                continue

            readable.append(channel)

        return tuple(readable)

    async def watch_source(
        self, thread_id: str, call_id: str, channel: JournalChannel
    ) -> WatchSource:
        """Слежение за каналом журнала вызова: по подписке на рост его
        ресурса, если сервер журнала известен и подключается; опрос сервера —
        страховка и запасной путь."""
        listened: tuple[McpToolServer, str] | None = None
        address = await self._journals.address(thread_id, call_id)
        if address is not None:
            if uris := self._media.journal(address.server):
                read = JournalRead(run=address.run, node=address.call, channel=channel)
                try:
                    port = await self._servers.port(address.server, self._caller)
                except McpClientError as exc:
                    logger.warning(
                        "journal watch: call %s of thread %s is polled, the "
                        "server is not reachable to listen: %s",
                        call_id,
                        thread_id,
                        exc,
                    )
                else:
                    listened = (port, uris.uri(read))

        return ListenedChannelWatch(self, thread_id, call_id, channel, listened)


class ListenedChannelWatch(WatchSource):
    """Слежение панели за каналом журнала вызова, исполненного сервером.

    Создаётся RemoteStreams.watch_source(). Рост канала сервер сообщает
    событием notifications/resources/updated подписки subscriptions/listen:
    задача подписки (attach_waker) ставит признак «изменилось» и будит
    сторож панели (CanvasWatch). Состояние канала — размер и признак конца —
    берётся пустым окном за концом журнала, текст при этом не читается;
    пока подписка жива, окно читается только после события. listened —
    порт сервера и uri канала для подписки; None — подписаться не на что
    (журнал сервера неизвестен или сервер недоступен), и источник, как и
    после потери подписки, опрашивается тактом сторожа.
    """

    BEYOND: ClassVar[int] = 1 << 62
    """Смещение заведомо за концом журнала: окно без текста, только координаты."""

    def __init__(
        self,
        streams: RemoteStreams,
        thread_id: str,
        call_id: str,
        channel: JournalChannel,
        listened: tuple[McpToolServer, str] | None,
    ) -> None:
        self._streams = streams
        self._thread_id = thread_id
        self._call_id = call_id
        self._channel = channel
        self._listened = listened
        self._listener: asyncio.Task[None] | None = None
        self._listening = False
        self._changed = True
        self._last: WatchProbe | None = None

    async def probe(self) -> WatchProbe | None:
        stale = self._changed
        if not self._listening:
            stale = True

        if not stale:
            return self._last

        self._changed = False
        try:
            piece = await self._streams.slice_at(
                self._thread_id, self._call_id, self._channel, self.BEYOND
            )
        except McpClientError as exc:
            self._changed = True
            msg = (
                f"probing channel {self._channel.value} of call {self._call_id} "
                f"in thread {self._thread_id} on its mcp server failed: {exc}"
            )
            raise WatchProbeError(msg) from exc

        if piece is None:
            return None

        self._last = WatchProbe(
            revision=piece.revision,
            size=piece.size,
            closed=piece.closed,
            final=piece.closed,
            note=piece.note,
        )
        return self._last

    def attach_waker(self) -> asyncio.Event | None:
        listened = self._listened
        if listened is None:
            return None

        port, uri = listened
        waker = asyncio.Event()
        self._listener = asyncio.create_task(
            self._listen(port, uri, waker), name=f"journal-listen:{self._call_id}"
        )
        return waker

    def detach_waker(self, event: asyncio.Event) -> None:
        listener = self._listener
        self._listener = None
        if listener is None:
            return

        listener.cancel()

    async def _listen(
        self, port: McpToolServer, uri: str, waker: asyncio.Event
    ) -> None:
        """Подписка на рост канала: событие — признак и будильник; сервер не
        подтвердил uri канала или подписка потеряна — в лог, источник
        возвращается к опросу."""
        notifications = mt.SubscriptionFilter(resource_subscriptions=[uri])
        try:
            async with port.listen(notifications) as subscription:
                honored = subscription.honored.resource_subscriptions
                if honored is None or uri not in honored:
                    logger.warning(
                        "journal watch: call %s of thread %s falls back to "
                        "polling, the server acknowledged %s instead of %s",
                        self._call_id,
                        self._thread_id,
                        honored,
                        uri,
                    )
                    return

                self._listening = True
                self._changed = True
                logger.info(
                    "journal watch: call %s of thread %s listens on %s",
                    self._call_id,
                    self._thread_id,
                    uri,
                )
                async for _ in subscription:
                    self._changed = True
                    waker.set()
        except (McpClientError, MCPError) as exc:
            logger.warning(
                "journal watch: call %s of thread %s falls back to polling, "
                "listening on %s failed: %s",
                self._call_id,
                self._thread_id,
                uri,
                exc,
            )
        finally:
            self._listening = False
            self._changed = True
