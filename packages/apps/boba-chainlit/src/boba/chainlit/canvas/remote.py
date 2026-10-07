"""Журналы вызовов, исполненных MCP-серверами: адреса и окна.

Вызов исполняет сервер, журнал лежит у него. Адрес журнала чат узнаёт из
итога вызова, а текст читает окнами через клиента MCP.

Ошибки:
McpClientError — сервер недоступен или ответил не окном журнала.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import ClassVar

from boba.canvas.canvas import WatchProbe, WatchSource
from boba.canvas.journal import StreamSlice
from boba.chainlit.chat.dialog import ThreadMessages, ToolReply
from boba.chainlit.rendering.chat_view import StreamableTools
from boba.mcp_client.client import McpCaller, McpClientError, McpServers
from boba.toolkit.channels import JournalChannel, JournalChannels
from boba.toolkit.dag import JournalAddress
from boba.toolkit.wire import JournalRead

__all__ = ["RemoteJournals", "RemoteStreams"]


logger = logging.getLogger(__name__)


class RemoteJournals(StreamableTools):
    """Адреса журналов вызовов, исполненных MCP-серверами.

    Объект один на процесс: его создаёт сборка чата. Журнал вызова лежит на
    сервере, который вызов исполнил; его адрес — сервер, запуск и
    идентификатор вызова на сервере — приходит в итоге вызова. Путь вызова
    узла (ChatCalls) запоминает адрес здесь, как только вызов закончился;
    адрес вызова, которого в памяти нет, берётся из истории треда: итог
    хранится там вместе с адресом. Пока вызов идёт, адреса нет и журнал не
    читается.
    """

    KEEP: ClassVar[int] = 512
    """Сколько адресов помнить в памяти; остальные находятся по истории."""

    def __init__(self, history: ThreadMessages) -> None:
        self._history = history
        self._known: OrderedDict[tuple[str, str], JournalAddress] = OrderedDict()
        self._streamable: frozenset[str] = frozenset()

    def mark_streamable(self, tool_names: frozenset[str]) -> None:
        """Инструменты подключённых серверов с журналом вызовов: набор
        только растёт — серверы подключаются по мере прихода сессий."""
        self._streamable = self._streamable | tool_names

    def streamable(self, tool_name: str) -> bool:
        return tool_name in self._streamable

    def remember(self, thread_id: str, call_id: str, address: JournalAddress) -> None:
        """Адрес журнала закончившегося вызова call_id треда."""
        key = (thread_id, call_id)
        self._known[key] = address
        self._known.move_to_end(key)
        while len(self._known) > self.KEEP:
            self._known.popitem(last=False)

    async def address(self, thread_id: str, call_id: str) -> JournalAddress | None:
        """Адрес журнала вызова: из памяти, иначе из истории треда; None —
        вызов ещё идёт, его исполнял не сервер с журналом либо запись
        истории сделана в формате, по которому журнал не прочитать."""
        known = self._known.get((thread_id, call_id))
        if known is not None:
            return known

        for message in await self._history.load(thread_id):
            if not isinstance(message, ToolReply):
                continue

            if address := message.journals().get(call_id):
                return address

        return None


class RemoteStreams:
    """Чтение журналов вызовов MCP-серверов для панели живого вывода.

    Создаётся обработчиком действия панели из реестра журналов, клиента MCP
    и пользователя сессии: сервер отдаёт журнал только тому, кто вызывал.
    Панель зовёт его, когда своего журнала у вызова нет: окно читает
    операция сервера, адрес журнала даёт реестр RemoteJournals.
    """

    def __init__(
        self, journals: RemoteJournals, servers: McpServers, caller: McpCaller
    ) -> None:
        self._journals = journals
        self._servers = servers
        self._caller = caller

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

    def watch_source(
        self, thread_id: str, call_id: str, channel: JournalChannel
    ) -> WatchSource:
        """Слежение за журналом вызова опросом сервера."""
        return PolledChannelWatch(self, thread_id, call_id, channel)


class PolledChannelWatch(WatchSource):
    """Слежение панели за каналом журнала опросом сервера.

    Создаётся RemoteStreams.watch_source(). Состояние канала — размер и
    признак конца — берётся пустым окном за концом журнала: текст при этом
    не читается. Будильника нет, панель опрашивает источник своим тактом.
    """

    BEYOND: ClassVar[int] = 1 << 62
    """Смещение заведомо за концом журнала: окно без текста, только координаты."""

    def __init__(
        self,
        streams: RemoteStreams,
        thread_id: str,
        call_id: str,
        channel: JournalChannel,
    ) -> None:
        self._streams = streams
        self._thread_id = thread_id
        self._call_id = call_id
        self._channel = channel

    async def probe(self) -> WatchProbe | None:
        try:
            piece = await self._streams.slice_at(
                self._thread_id, self._call_id, self._channel, self.BEYOND
            )
        except McpClientError as exc:
            logger.warning(
                "journal watch: probing call %s of thread %s failed: %s",
                self._call_id,
                self._thread_id,
                exc,
            )
            return None

        if piece is None:
            return None

        return WatchProbe(
            revision=f"{piece.size}:{int(piece.closed)}",
            size=piece.size,
            closed=piece.closed,
            final=piece.closed,
            note=piece.note,
        )

    def attach_waker(self) -> asyncio.Event | None:
        return None

    def detach_waker(self, event: asyncio.Event) -> None:
        return None
