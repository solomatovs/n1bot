"""Журналы вызовов, исполненных MCP-серверами: живое состояние и окна.

Вызов исполняет сервер, журнал лежит у него. Чат узнаёт о росте журнала
сигналами клиента MCP, а текст читает окнами через того же клиента.

Ошибки:
McpClientError — сервер недоступен или ответил не окном журнала.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import ClassVar

from langchain_core.messages import ToolCall, ToolMessage

from boba.canvas.canvas import WatchProbe, WatchSource
from boba.canvas.journal import StreamSlice
from boba.chainlit.chat.history import ThreadMessages
from boba.identity.context import CallContexts
from boba.mcp_client.client import (
    CallSignals,
    JournalAddress,
    JournalAddresses,
    JournalListener,
    McpCaller,
    McpServers,
)
from boba.toolkit.channels import JournalChannel, JournalChannels
from boba.toolkit.wire import JournalRead, JournalSignal

__all__ = ["RemoteJournal", "RemoteJournals", "RemoteStreams"]


class RemoteJournal:
    """Живое состояние журнала одного вызова на MCP-сервере.

    Создаётся реестром RemoteJournals по первому сигналу вызова и хранит
    последнее известное состояние каналов. Слежение панели за каналом
    (RemoteChannelWatch) читает состояние отсюда; сигнал будит ждущих, как
    запись в свой журнал.
    """

    def __init__(self, address: JournalAddress) -> None:
        self.address = address
        self._probes: dict[str, WatchProbe] = {}
        self._wakers: set[asyncio.Event] = set()

    def take(self, signal: JournalSignal) -> None:
        self._probes[signal.channel] = WatchProbe(
            revision=f"{signal.size}:{int(signal.closed)}",
            size=signal.size,
            closed=signal.closed,
            final=signal.closed,
            note=signal.note,
        )
        for waker in self._wakers:
            waker.set()

    def channels(self) -> tuple[JournalChannel, ...]:
        """Видимые каналы, о которых сервер уже сообщал."""
        known: list[JournalChannel] = []
        for channel in JournalChannels.VISIBLE:
            if channel.value in self._probes:
                known.append(channel)

        return tuple(known)

    def watched(self, channel: JournalChannel) -> RemoteChannelWatch:
        return RemoteChannelWatch(self, channel)

    def probe_of(self, channel: JournalChannel) -> WatchProbe | None:
        return self._probes.get(channel.value)

    def attach_waker(self) -> asyncio.Event:
        waker = asyncio.Event()
        self._wakers.add(waker)

        return waker

    def detach_waker(self, event: asyncio.Event) -> None:
        self._wakers.discard(event)


class RemoteChannelWatch(WatchSource):
    """Слежение панели за одним каналом журнала вызова на MCP-сервере.

    Создаётся RemoteJournal.watched(); состояние канала берёт из сигналов,
    собранных журналом, будильник — его же.
    """

    def __init__(self, journal: RemoteJournal, channel: JournalChannel) -> None:
        self._journal = journal
        self._channel = channel

    async def probe(self) -> WatchProbe | None:
        return self._journal.probe_of(self._channel)

    def attach_waker(self) -> asyncio.Event:
        return self._journal.attach_waker()

    def detach_waker(self, event: asyncio.Event) -> None:
        self._journal.detach_waker(event)


class CallJournalListener(JournalListener):
    """Приёмник сигналов одного вызова: передаёт их реестру вместе с тредом
    и сервером вызова. Создаётся RemoteJournals.listener() в контексте
    вызова; сигналы приходят позже, из задачи сессии MCP."""

    def __init__(self, journals: RemoteJournals, thread_id: str, server: str) -> None:
        self._journals = journals
        self._thread_id = thread_id
        self._server = server

    async def appended(self, signal: JournalSignal) -> None:
        self._journals.take(self._thread_id, self._server, signal)


class RemoteJournals(CallSignals):
    """Реестр журналов вызовов, исполненных MCP-серверами.

    Реализация CallSignals клиента MCP: объект один на процесс, его создаёт
    сборка чата и отдаёт клиенту. По сигналам роста ведёт живое состояние
    журналов (ключ — тред и узел вызова) для панели живого вывода. Адрес
    журнала вызова, которого в памяти нет, берётся из истории треда: клиент
    кладёт его в сообщение инструмента.
    """

    KEEP: ClassVar[int] = 512
    """Сколько журналов помнить в памяти; остальные находятся по истории."""

    def __init__(self, contexts: CallContexts, history: ThreadMessages) -> None:
        self._contexts = contexts
        self._history = history
        self._addresses = JournalAddresses()
        self._live: OrderedDict[tuple[str, str], RemoteJournal] = OrderedDict()

    def listener(self, server: str, call: ToolCall) -> JournalListener:
        thread_id = self._contexts.current().scope.id

        return CallJournalListener(self, thread_id, server)

    def take(self, thread_id: str, server: str, signal: JournalSignal) -> None:
        key = (thread_id, signal.node)
        journal = self._live.get(key)
        if journal is None:
            journal = RemoteJournal(JournalAddress(server=server, run=signal.run))
            self._live[key] = journal

        self._live.move_to_end(key)
        while len(self._live) > self.KEEP:
            self._live.popitem(last=False)

        journal.take(signal)

    def live(self, thread_id: str, call_id: str) -> RemoteJournal | None:
        return self._live.get((thread_id, call_id))

    async def address(self, thread_id: str, call_id: str) -> JournalAddress | None:
        """Адрес журнала вызова: из памяти, иначе из истории треда; None —
        вызов исполнял не сервер с журналом."""
        journal = self.live(thread_id, call_id)
        if journal is not None:
            return journal.address

        for message in await self._history.load(thread_id):
            if not isinstance(message, ToolMessage):
                continue

            if message.tool_call_id != call_id:
                continue

            return self._addresses.of(message)

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
            run=address.run, node=call_id, channel=channel, offset=offset
        )

        return await self._servers.journal(address.server, self._caller, request)

    async def slice_before(
        self, thread_id: str, call_id: str, channel: JournalChannel, end: int
    ) -> StreamSlice | None:
        address = await self._journals.address(thread_id, call_id)
        if address is None:
            return None

        request = JournalRead(
            run=address.run, node=call_id, channel=channel, before=end
        )

        return await self._servers.journal(address.server, self._caller, request)

    async def channels(
        self, thread_id: str, call_id: str
    ) -> tuple[JournalChannel, ...]:
        """Видимые каналы журнала вызова: по сигналам живого вызова, иначе
        те, что сервер отдаёт окном."""
        journal = self._journals.live(thread_id, call_id)
        if journal is not None:
            return journal.channels()

        readable: list[JournalChannel] = []
        for channel in JournalChannels.VISIBLE:
            piece = await self.slice_at(thread_id, call_id, channel, 0)
            if piece is None:
                continue

            readable.append(channel)

        return tuple(readable)

    def watch_source(
        self, thread_id: str, call_id: str, channel: JournalChannel
    ) -> WatchSource | None:
        """Слежение за живым журналом; None — вызов уже не в памяти, его
        журнал закрыт и следить не за чем."""
        journal = self._journals.live(thread_id, call_id)
        if journal is None:
            return None

        return journal.watched(channel)
