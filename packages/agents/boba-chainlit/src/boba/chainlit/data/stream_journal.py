"""Журнал вывода инструментов: файл на канал вызова в томе пользователя.

Вызов пишет {thread}/{call_id}.{tool}.{channel}.log по файлу на канал и один
сайдкар {call_id}.meta.json с итогом; чтение — окнами по смещению в файле.
Единица учёта и вытеснения — вызов целиком: все его файлы вместе.

Ошибки: StreamJournalError — журнал не открылся, окно не читается, журналы
треда не удаляются; сбои записи гасятся внутрь, закрывая поток пометкой.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from typing import ClassVar

from boba.chainlit.domain.stream import (
    CallLogUsage,
    JournalFile,
    JournalText,
    JournalWindow,
    StreamJournalError,
    StreamKey,
    StreamMeta,
    StreamRecorderPort,
    StreamSlice,
    StreamStorePort,
    ThreadUsage,
    VaultUsage,
)
from boba.toolkit.channels import ToolChannel

__all__ = [
    "DirVault",
    "StreamJournal",
    "StreamRecorder",
]

logger = logging.getLogger(__name__)


class DirVault:
    """Том-каталог: без монтирований и привилегий, квоту держит журнал."""

    def __init__(self, root: str) -> None:
        self._root = root

    def root_for(self, user_id: str) -> str:
        path = os.path.join(self._root, user_id)
        os.makedirs(path, exist_ok=True)
        return path


class StreamRecorder(StreamRecorderPort):
    """Писатель журнала одного вызова: append по мере работы инструмента.

    Реализует StreamSink: сбой записи (кончилось место, недоступен том) не
    выходит наружу — журнал закрывается пометкой, инструмент работает дальше.
    on_data зовётся после каждой порции и при закрытии.
    """

    FILE_MODE: ClassVar[int] = 0o600

    def __init__(
        self,
        log_path: str,
        meta_path: str,
        tool_name: str,
        on_data: Callable[[], None],
    ) -> None:
        self._log_path = log_path
        self._meta_path = meta_path
        self._on_data = on_data
        self._lock = threading.Lock()
        self._closed = False
        self._meta = StreamMeta(tool_name=tool_name)

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        self._fd = os.open(log_path, flags, self.FILE_MODE)
        self._size = os.fstat(self._fd).st_size
        self._write_meta()

    @property
    def log_path(self) -> str:
        return self._log_path

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, data: bytes) -> None:
        if not data:
            return

        note = ""
        with self._lock:
            if self._closed:
                return

            note = self._append(data)

        if note:
            self.close(note)
            return

        self._on_data()

    def _append(self, data: bytes) -> str:
        """Дописать порцию под локом; непустая строка — причина закрытия."""
        try:
            os.write(self._fd, data)
            self._size += len(data)
        except OSError as exc:
            logger.warning("stream journal write failed: %s: %s", self._log_path, exc)
            return f"journal stopped: {exc.strerror or exc}"

        return ""

    def feed_text(self, text: str) -> None:
        self.feed(JournalText.encode(text))

    def close(self, note: str) -> None:
        """Идемпотентное закрытие: первый вызов фиксирует итог в сайдкаре."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._meta = self._meta.model_copy(update={"closed": True, "note": note})
            os.close(self._fd)

        self._write_meta()
        self._on_data()

    def tail(self, window: int) -> StreamSlice:
        with self._lock:
            meta = self._meta
            size = self._size

        view = StreamFileView(self._log_path)
        return view.slice_before(size, window, size, meta)

    def _write_meta(self) -> None:
        """Сайдкар атомарно: rename не оставит битого json при падении."""
        tmp = JournalFile.tmp_of(self._meta_path)

        try:
            with open(tmp, "w", encoding=JournalText.ENCODING) as f:
                json.dump(self._meta.model_dump(), f, ensure_ascii=False)
            os.rename(tmp, self._meta_path)
        except OSError:
            logger.warning("stream meta is not written: %s", self._meta_path)


class StreamFileView:
    """Чтение окон журнала по смещению: файл любого размера, память — окно.

    Окна выравниваются по границам строк, чтобы фронт склеивал их встык без
    рваных строк: голова окна сдвигается за первый перевод строки (кроме
    начала файла), хвост forward-окна обрезается по последнему (кроме конца
    файла). Строка длиннее окна отдаётся как есть — прогресс важнее красоты.
    """

    def __init__(self, log_path: str) -> None:
        self._log_path = log_path

    def slice_at(
        self, offset: int, window: int, size: int, meta: StreamMeta
    ) -> StreamSlice:
        """Окно вперёд от смещения: [выровненный offset, граница строки)."""
        start = max(0, min(offset, size))
        start, data = self._aligned_read(start, min(window, size - start))

        end = start + len(data)
        if end < size:
            cut = data.rfind(b"\n")
            if 0 <= cut < len(data) - 1:
                data = data[: cut + 1]
                end = start + len(data)

        return self._slice(data, start, end, size, window, meta)

    def slice_before(
        self, end: int, window: int, size: int, meta: StreamMeta
    ) -> StreamSlice:
        """Окно, заканчивающееся ровно на end: стык для прокрутки вверх."""
        stop = max(0, min(end, size))
        start = max(0, stop - window)
        start, data = self._aligned_read(start, stop - start)

        return self._slice(data, start, start + len(data), size, window, meta)

    def _aligned_read(self, start: int, length: int) -> tuple[int, bytes]:
        """Чтение с головой на границе строки.

        Смещение, стоящее сразу за переводом строки (стык окон), не трогается;
        произвольное — сдвигается за первый перевод строки внутри окна. Без
        переводов строки вовсе (одна строка длиннее окна) — как есть.
        """
        if length <= 0:
            return start, b""

        if start == 0:
            return start, self._read(0, length)

        raw = self._read(start - 1, length + 1)
        data = raw[1:]
        if raw[:1] == b"\n":
            return start, data

        cut = data.find(b"\n")
        if cut < 0 or cut == len(data) - 1:
            return start, data

        return start + cut + 1, data[cut + 1 :]

    def _read(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""

        fd = os.open(self._log_path, os.O_RDONLY)
        try:
            return os.pread(fd, length, start)
        finally:
            os.close(fd)

    @staticmethod
    def _slice(  # noqa: PLR0913
        data: bytes,
        start: int,
        end: int,
        size: int,
        window: int,
        meta: StreamMeta,
    ) -> StreamSlice:
        return StreamSlice(
            text=JournalText.decode(data),
            offset=start,
            end=end,
            size=size,
            window=window,
            closed=meta.closed,
            note=meta.note,
        )


class StreamJournal(StreamStorePort):
    """Журналы вызовов в служебном томе: запись рекордером, чтение окнами.

    Перед открытием нового журнала держится резерв места: старейшие по
    записи треды вытесняются, пока резерв не появится; защищённые треды
    (текущий и с живыми потоками) ротация не трогает. Общий потолок держит
    точка монтирования под корнем журналов: её размер и есть квота.
    """

    def __init__(self, vault: DirVault, reserve_bytes: int) -> None:
        if reserve_bytes < 0:
            msg = f"reserve_bytes must be >= 0, got {reserve_bytes}"
            raise ValueError(msg)

        self._vault = vault
        self._reserve = reserve_bytes

    def recorder(
        self,
        key: StreamKey,
        tool_name: str,
        channel: ToolChannel,
        on_data: Callable[[], None],
        protected_prefixes: frozenset[str],
    ) -> StreamRecorder:
        """Открыть журнал канала на запись; каталог треда создаётся здесь.

        protected_prefixes — префиксы {thread}/{call_id}. живых вызовов,
        вытеснять их файлы нельзя; открываемый вызов защищён всегда. Место
        кончилось — старейшие закрытые вызовы вытесняются до успеха.
        """
        root = self._vault.root_for(key.user_id)
        protected = protected_prefixes | {key.call_prefix()}

        self._ensure_reserve(root, protected)

        log_path = os.path.join(root, key.rel_log(tool_name, channel))

        # вместо релея stderr в общий журнал — одна строка о месте записи
        logger.info("tool stream journal: %s -> %s", tool_name, log_path)

        return self._open_recorder(key, root, tool_name, channel, on_data, protected)

    def _open_recorder(  # noqa: PLR0913
        self,
        key: StreamKey,
        root: str,
        tool_name: str,
        channel: ToolChannel,
        on_data: Callable[[], None],
        protected: frozenset[str],
    ) -> StreamRecorder:
        """Открыть рекордер, вытесняя старые вызовы при ENOSPC до успеха.

        Каталог треда создаётся в цикле: вытеснение могло снести опустевший
        каталог того же треда, куда пишем.
        """
        log_path = os.path.join(root, key.rel_log(tool_name, channel))
        meta_path = os.path.join(root, key.rel_meta())

        while True:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                return StreamRecorder(log_path, meta_path, tool_name, on_data)
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise StreamJournalError(
                        f"stream log is not writable: {log_path}: {exc}"
                    ) from exc

                freed = self._evict_oldest(root, protected)
                if freed < 0:
                    raise StreamJournalError(
                        f"stream vault is full, nothing to evict: {log_path}: {exc}"
                    ) from exc

                logger.info(
                    "stream journal evicted %d bytes to open %s",
                    freed,
                    key.call_id,
                )

    def usage(self, user_id: str) -> VaultUsage:
        """Занятость тома: место и журналы по тредам, старые первыми."""
        root = self._vault.root_for(user_id)
        space = os.statvfs(root)

        threads = sorted(self._thread_usages(root), key=lambda t: t.last_write_at)

        return VaultUsage(
            total_bytes=space.f_blocks * space.f_frsize,
            free_bytes=space.f_bavail * space.f_frsize,
            threads=tuple(threads),
        )

    def purge_thread(self, user_id: str, thread_id: str) -> int:
        """Удалить журналы треда; возвращает освобождённые байты."""
        root = self._vault.root_for(user_id)
        path = os.path.join(root, thread_id)

        freed = 0
        for entry in self._thread_usages(root):
            if entry.thread_id == thread_id:
                freed = entry.bytes_used

        if freed == 0 and not os.path.isdir(path):
            return 0

        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise StreamJournalError(
                f"stream journal purge failed: {path}: {exc}"
            ) from exc

        return freed

    def _ensure_reserve(self, root: str, protected: frozenset[str]) -> None:
        """LRU-вытеснение логов до резерва; нечего вытеснять — предупредить."""
        if not self._reserve:
            return

        while self._free_bytes(root) < self._reserve:
            freed = self._evict_oldest(root, protected)
            if freed < 0:
                logger.warning(
                    "stream journal reserve is not met: %d < %d",
                    self._free_bytes(root),
                    self._reserve,
                )
                return

    def _evict_oldest(self, root: str, protected: frozenset[str]) -> int:
        """Удалить старейший незащищённый лог; -1 — вытеснять нечего."""
        victim = min(
            self._evictable(root, protected),
            default=None,
            key=lambda entry: entry.last_write_at,
        )
        if victim is None:
            return -1

        self._remove_call(root, victim)

        logger.info(
            "stream journal evicted call %s of thread %s (%d bytes)",
            victim.call_id,
            victim.thread_id,
            victim.bytes_used,
        )
        return victim.bytes_used

    def _evictable(
        self, root: str, protected: frozenset[str]
    ) -> Iterator[CallLogUsage]:
        for entry in self._call_logs(root):
            if entry.prefix in protected:
                continue

            yield entry

    def _remove_call(self, root: str, entry: CallLogUsage) -> None:
        """Снести все файлы вызова; опустевший каталог треда убрать."""
        for rel in entry.rel_files:
            try:
                os.remove(os.path.join(root, rel))
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning(
                    "stream journal eviction failed on %s", rel, exc_info=True
                )

        try:
            os.rmdir(os.path.join(root, entry.thread_id))
        except OSError:
            return

    @staticmethod
    def _call_files(thread_path: str) -> Iterator[tuple[str, str, os.stat_result]]:
        """Файлы каталога треда: (call_id, имя, stat); чужие имена — мимо."""
        for file_entry in os.scandir(thread_path):
            if not file_entry.is_file(follow_symlinks=False):
                continue

            name = file_entry.name
            if JournalFile.is_log(name):
                yield JournalFile.parse_log(name).call_id, name, file_entry.stat()
                continue

            if JournalFile.is_meta(name):
                yield JournalFile.call_id_of_meta(name), name, file_entry.stat()

    @classmethod
    def _call_logs(cls, root: str) -> Iterator[CallLogUsage]:
        """Файлы каждого вызова одной записью: единица учёта — вызов."""
        for thread_entry in os.scandir(root):
            if not thread_entry.is_dir(follow_symlinks=False):
                continue

            grouped: dict[str, list[tuple[str, os.stat_result]]] = {}
            for call_id, name, stat in cls._call_files(thread_entry.path):
                grouped.setdefault(call_id, []).append((name, stat))

            for call_id, files in grouped.items():
                rel_files: list[str] = []
                used = 0
                last_write = 0.0
                for name, stat in files:
                    rel_files.append(f"{thread_entry.name}/{name}")
                    used += stat.st_size
                    last_write = max(last_write, stat.st_mtime)

                yield CallLogUsage(
                    thread_id=thread_entry.name,
                    call_id=call_id,
                    rel_files=tuple(sorted(rel_files)),
                    bytes_used=used,
                    last_write_at=last_write,
                )

    @staticmethod
    def _free_bytes(root: str) -> int:
        space = os.statvfs(root)
        return space.f_bavail * space.f_frsize

    @staticmethod
    def _thread_usages(root: str) -> Iterator[ThreadUsage]:
        for thread_entry in os.scandir(root):
            if not thread_entry.is_dir(follow_symlinks=False):
                continue

            used = 0
            call_ids: set[str] = set()
            last_write = 0.0
            for file_entry in os.scandir(thread_entry.path):
                if not file_entry.is_file(follow_symlinks=False):
                    continue
                stat = file_entry.stat()
                used += stat.st_size
                last_write = max(last_write, stat.st_mtime)
                if JournalFile.is_log(file_entry.name):
                    call_ids.add(JournalFile.parse_log(file_entry.name).call_id)
            calls = len(call_ids)

            yield ThreadUsage(
                thread_id=thread_entry.name,
                bytes_used=used,
                calls=calls,
                last_write_at=last_write,
            )

    def slice_at(
        self, key: StreamKey, offset: int, channel: ToolChannel
    ) -> StreamSlice | None:
        """Окно записанного журнала вперёд; None — журнала такого вызова нет.

        offset меньше нуля — хвост файла.
        """
        opened = self._open_view(key, channel)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            if offset < 0:
                return view.slice_before(size, JournalWindow.BYTES, size, meta)
            return view.slice_at(offset, JournalWindow.BYTES, size, meta)
        except OSError as exc:
            raise StreamJournalError(
                f"stream log is not readable: {key.call_id}/{channel}: {exc}"
            ) from exc

    def slice_before(
        self, key: StreamKey, end: int, channel: ToolChannel
    ) -> StreamSlice | None:
        """Окно, заканчивающееся на end: прокрутка вверх стыкуется встык."""
        opened = self._open_view(key, channel)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            return view.slice_before(end, JournalWindow.BYTES, size, meta)
        except OSError as exc:
            raise StreamJournalError(
                f"stream log is not readable: {key.call_id}/{channel}: {exc}"
            ) from exc

    def log_rel_path(self, key: StreamKey, channel: ToolChannel) -> str | None:
        """Rel-путь лога канала; имя инструмента берётся из сайдкара вызова.

        None — вызов не журналировался (нет сайдкара) либо файла канала нет.
        """
        root = self._vault.root_for(key.user_id)
        meta = self._read_meta(os.path.join(root, key.rel_meta()))
        if not meta.tool_name:
            return None

        rel = key.rel_log(meta.tool_name, channel)
        if not os.path.isfile(os.path.join(root, rel)):
            return None

        return rel

    def _open_view(
        self, key: StreamKey, channel: ToolChannel
    ) -> tuple[StreamFileView, int, StreamMeta] | None:
        root = self._vault.root_for(key.user_id)

        meta = self._read_meta(os.path.join(root, key.rel_meta()))
        if not meta.tool_name:
            return None

        log_path = os.path.join(root, key.rel_log(meta.tool_name, channel))

        try:
            size = os.stat(log_path).st_size
        except FileNotFoundError:
            return None

        return StreamFileView(log_path), size, meta

    def vault_root(self, user_id: str) -> str:
        """Корень тома пользователя: под ним лежат файлы каналов вызовов.

        Отдаётся скачиванию журнала — файл течёт из тома тем же StreamedFile,
        что и вложения.
        """
        return self._vault.root_for(user_id)

    @staticmethod
    def _read_meta(meta_path: str) -> StreamMeta:
        """Сайдкар потерян или бит — журнал считается закрытым без итога."""
        try:
            with open(meta_path, encoding=JournalText.ENCODING) as f:
                raw = json.load(f)
            return StreamMeta.model_validate(raw)
        except (OSError, ValueError):
            return StreamMeta(tool_name="", closed=True, note="")
