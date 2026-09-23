"""Документ как последовательность страниц: виды форматов, окно чтения, поток
байтов на входе и контракт открытого документа.

Реализации по форматам живут в boba.doc.readers, OCR — в boba.doc.ocr, точка
входа — DocumentRouter в boba.doc.router.

Ошибки:
DocumentError — документ не распознан, не открыт или страница не прочитана.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from PIL import Image, UnidentifiedImageError

__all__ = [
    "BoxedHit",
    "ByteStream",
    "DisabledOcr",
    "Document",
    "DocumentError",
    "DocumentHint",
    "DocumentKind",
    "Formats",
    "Hit",
    "OcrEngine",
    "PageInfo",
    "PageWindow",
    "PagedDocument",
    "ParsedPage",
    "Prefixed",
    "Sha256Stream",
    "SizedPageInfo",
    "Spool",
    "TextSearch",
]


class DocumentError(Exception):
    """Документ не распознан, не открыт или страница не прочитана."""


class ByteStream(Protocol):
    """Источник байтов документа: достаточно read, seek не требуется.

    Подходит открытый файл, сокет через makefile, конец пипы через fdopen,
    BytesIO. Как буферизовать байты — решает ридер формата, не вызывающий.
    """

    def read(self, size: int = -1, /) -> bytes: ...


class DocumentKind(StrEnum):
    """Вид документа: определяет ридер, который его откроет."""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    XLS = "xls"
    RTF = "rtf"
    IMAGE = "image"
    TEXT = "text"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DocumentHint:
    """Что известно о документе до байтов: media_type и имя файла.

    Оба поля могут быть пустыми — тогда вид определяется по первым байтам.
    """

    media_type: str = ""
    filename: str = ""


class Formats:
    """Карта форматов: media_type и суффикс имени дают вид документа, первые
    байты файла — запасной способ, когда транспорт отдал octet-stream."""

    HEAD_SIZE: ClassVar[int] = 8192
    IMAGE_PREFIX: ClassVar[str] = "image/"
    TEXT_PREFIX: ClassVar[str] = "text/"
    MEDIA_TYPE_SEPARATOR: ClassVar[str] = ";"
    SUFFIX_SEPARATOR: ClassVar[str] = "."
    ZIP_MAGIC: ClassVar[bytes] = b"PK\x03\x04"

    BY_MEDIA_TYPE: ClassVar[Mapping[str, DocumentKind]] = {
        "application/pdf": DocumentKind.PDF,
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document": DocumentKind.DOCX,
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet": DocumentKind.XLSX,
        "application/vnd.openxmlformats-officedocument."
        "presentationml.presentation": DocumentKind.PPTX,
        "application/vnd.ms-excel": DocumentKind.XLS,
        "application/rtf": DocumentKind.RTF,
        "text/rtf": DocumentKind.RTF,
        "application/json": DocumentKind.TEXT,
        "application/xml": DocumentKind.TEXT,
        "application/x-yaml": DocumentKind.TEXT,
    }

    BY_SUFFIX: ClassVar[Mapping[str, DocumentKind]] = {
        "pdf": DocumentKind.PDF,
        "docx": DocumentKind.DOCX,
        "xlsx": DocumentKind.XLSX,
        "xlsm": DocumentKind.XLSX,
        "pptx": DocumentKind.PPTX,
        "xls": DocumentKind.XLS,
        "rtf": DocumentKind.RTF,
        "png": DocumentKind.IMAGE,
        "jpg": DocumentKind.IMAGE,
        "jpeg": DocumentKind.IMAGE,
        "gif": DocumentKind.IMAGE,
        "bmp": DocumentKind.IMAGE,
        "webp": DocumentKind.IMAGE,
        "tif": DocumentKind.IMAGE,
        "tiff": DocumentKind.IMAGE,
        "txt": DocumentKind.TEXT,
        "md": DocumentKind.TEXT,
        "csv": DocumentKind.TEXT,
        "tsv": DocumentKind.TEXT,
        "log": DocumentKind.TEXT,
        "json": DocumentKind.TEXT,
        "xml": DocumentKind.TEXT,
        "yaml": DocumentKind.TEXT,
        "yml": DocumentKind.TEXT,
        "sql": DocumentKind.TEXT,
    }

    BY_MAGIC: ClassVar[Mapping[bytes, DocumentKind]] = {
        b"%PDF": DocumentKind.PDF,
        b"{\\rtf": DocumentKind.RTF,
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": DocumentKind.XLS,
    }

    BY_ZIP_ENTRY: ClassVar[Mapping[bytes, DocumentKind]] = {
        b"word/": DocumentKind.DOCX,
        b"xl/": DocumentKind.XLSX,
        b"ppt/": DocumentKind.PPTX,
    }

    IMAGE_MEDIA_TYPES: ClassVar[tuple[str, ...]] = (
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/bmp",
        "image/webp",
        "image/tiff",
    )

    @classmethod
    def media_types(cls) -> tuple[str, ...]:
        """Явные media_type документов для маршрутов по типу: карта плюс
        картинки; текстовые text/* остаются за текстовыми ридерами."""
        return tuple(cls.BY_MEDIA_TYPE) + cls.IMAGE_MEDIA_TYPES

    @classmethod
    def normalize(cls, media_type: str) -> str:
        """media_type без параметров (`; charset=...`), в нижнем регистре."""
        base = media_type.split(cls.MEDIA_TYPE_SEPARATOR, 1)[0]

        return base.strip().lower()

    @classmethod
    def of_media_type(cls, media_type: str) -> DocumentKind:
        normalized = cls.normalize(media_type)
        if not normalized:
            return DocumentKind.UNKNOWN

        if normalized.startswith(cls.IMAGE_PREFIX):
            return DocumentKind.IMAGE

        if normalized.startswith(cls.TEXT_PREFIX):
            return DocumentKind.TEXT

        return cls.BY_MEDIA_TYPE.get(normalized, DocumentKind.UNKNOWN)

    @classmethod
    def of_filename(cls, filename: str) -> DocumentKind:
        if cls.SUFFIX_SEPARATOR not in filename:
            return DocumentKind.UNKNOWN

        suffix = filename.rsplit(cls.SUFFIX_SEPARATOR, 1)[1].lower()

        return cls.BY_SUFFIX.get(suffix, DocumentKind.UNKNOWN)

    @classmethod
    def of_hint(cls, hint: DocumentHint) -> DocumentKind:
        """Вид по подсказке транспорта; media_type главнее имени файла."""
        kind = cls.of_media_type(hint.media_type)
        if kind is not DocumentKind.UNKNOWN:
            return kind

        return cls.of_filename(hint.filename)

    @classmethod
    def detect(cls, hint: DocumentHint, head: bytes) -> DocumentKind:
        """Вид по подсказке, а когда её нет — по первым байтам файла."""
        kind = cls.of_hint(hint)
        if kind is not DocumentKind.UNKNOWN:
            return kind

        return cls.sniff(head)

    @classmethod
    def sniff(cls, head: bytes) -> DocumentKind:
        for magic, kind in cls.BY_MAGIC.items():
            if head.startswith(magic):
                return kind

        if head.startswith(cls.ZIP_MAGIC):
            return cls._sniff_zip(head)

        if cls._is_image(head):
            return DocumentKind.IMAGE

        return DocumentKind.UNKNOWN

    @classmethod
    def _sniff_zip(cls, head: bytes) -> DocumentKind:
        """Office-документ — zip, чей вид виден по именам первых записей."""
        for entry, kind in cls.BY_ZIP_ENTRY.items():
            if entry in head:
                return kind

        return DocumentKind.UNKNOWN

    @staticmethod
    def _is_image(head: bytes) -> bool:
        try:
            with Image.open(io.BytesIO(head)):
                return True
        except (UnidentifiedImageError, OSError, ValueError):
            return False


@dataclass(frozen=True)
class PageWindow:
    """Окно чтения: страницы с start по start + count - 1, нумерация с единицы."""

    ALL: ClassVar[int] = 1 << 31

    start: int
    count: int

    def __post_init__(self) -> None:
        if self.start < 1:
            raise DocumentError(f"page window: start must be >= 1, got {self.start}")

        if self.count < 1:
            raise DocumentError(f"page window: count must be >= 1, got {self.count}")

    @classmethod
    def whole(cls) -> PageWindow:
        return cls(start=1, count=cls.ALL)

    @classmethod
    def parse_many(cls, spec: str) -> tuple[PageWindow, ...]:
        """Окна из строки вида '1-5,10,15-20': диапазон или номер через запятую."""
        windows: list[PageWindow] = []
        for part in spec.split(","):
            piece = part.strip()
            if not piece:
                raise DocumentError(f"pages {spec!r}: empty item in the list")

            windows.append(cls._parse_one(piece, spec))

        return tuple(windows)

    @classmethod
    def _parse_one(cls, piece: str, spec: str) -> PageWindow:
        first, separator, last = piece.partition("-")
        try:
            start = int(first)
            stop = start
            if separator:
                stop = int(last)
        except ValueError as exc:
            raise DocumentError(
                f"pages {spec!r}: expected numbers and ranges like 1-5,10, got "
                f"{piece!r}"
            ) from exc

        if start < 1:
            raise DocumentError(f"pages {spec!r}: page numbers start at 1, got {start}")

        if stop < start:
            raise DocumentError(
                f"pages {spec!r}: range {piece!r} ends before it starts"
            )

        return cls(start=start, count=stop - start + 1)

    def numbers(self, page_count: int) -> range:
        """Номера страниц окна, которые есть в документе."""
        last = min(self.start + self.count - 1, page_count)

        return range(self.start, last + 1)


@dataclass(frozen=True)
class PageInfo:
    """Строка карты документа: номер страницы и объём текста на ней."""

    number: int
    chars: int


@dataclass(frozen=True)
class SizedPageInfo(PageInfo):
    """Страница с геометрией: pdf в пунктах, картинка в пикселях."""

    width: float
    height: float


@dataclass(frozen=True)
class ParsedPage:
    """Текст одной страницы документа."""

    number: int
    text: str


@dataclass(frozen=True)
class Hit:
    """Совпадение поиска: страница, смещение в её тексте и сниппет вокруг."""

    page: int
    offset: int
    length: int
    snippet: str


@dataclass(frozen=True)
class BoxedHit(Hit):
    """Совпадение с координатами на странице pdf в пунктах, начало координат
    в левом нижнем углу."""

    x: float
    y: float
    width: float
    height: float


class OcrEngine(Protocol):
    """Распознавание текста на картинке: страница без текстового слоя, картинка
    внутри страницы или вложение-изображение приходят сюда как PIL-образ.
    По enabled ридер решает, стоит ли вообще доставать картинки."""

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def recognize(self, image: Image.Image) -> str: ...


class DisabledOcr(OcrEngine):
    """OCR выключен конфигом: картинки и сканы дают пустой текст."""

    @property
    def enabled(self) -> bool:
        return False

    def recognize(self, image: Image.Image) -> str:
        return ""


class Document(Protocol):
    """Открытый документ: живёт, пока вызывающий читает его окнами.

    Роутер открывает документ по потоку и закрывает по выходу из контекста;
    страницы читаются окнами, поиск идёт по тексту окна.
    """

    @property
    @abstractmethod
    def kind(self) -> DocumentKind: ...

    @abstractmethod
    def page_count(self) -> int: ...

    @abstractmethod
    def outline(self) -> Sequence[PageInfo]: ...

    @abstractmethod
    def pages(self, window: PageWindow) -> Iterator[ParsedPage]: ...

    @abstractmethod
    def search(
        self, query: str, window: PageWindow, *, case_sensitive: bool, context: int
    ) -> Iterator[Hit]: ...

    @abstractmethod
    def close(self) -> None: ...


class Spool:
    """Буфер ридера для форматов с произвольным доступом (zip, pdf): до
    memory_limit байт в памяти, дальше безымянный временный файл."""

    CHUNK: ClassVar[int] = 1 << 20

    @classmethod
    def fill(
        cls, stream: ByteStream, memory_limit: int
    ) -> tempfile.SpooledTemporaryFile[bytes]:
        spool = tempfile.SpooledTemporaryFile(max_size=memory_limit)  # noqa: SIM115 — закрывает документ
        while chunk := stream.read(cls.CHUNK):
            spool.write(chunk)

        spool.seek(0)

        return spool

    @classmethod
    def drain(cls, stream: ByteStream) -> bytes:
        """Весь остаток потока в память: для форматов, которым нужны байты."""
        buffer = io.BytesIO()
        while chunk := stream.read(cls.CHUNK):
            buffer.write(chunk)

        return buffer.getvalue()


class Prefixed(ByteStream):
    """Поток с возвращённым началом: роутер прочитал голову для определения
    вида, ридер получает её первой, остальное — из исходного потока."""

    def __init__(self, head: bytes, rest: ByteStream) -> None:
        self._head = head
        self._rest = rest

    def read(self, size: int = -1, /) -> bytes:
        if size < 0:
            head = self._head
            self._head = b""
            return head + self._rest.read()

        if self._head:
            piece = self._head[:size]
            self._head = self._head[size:]
            return piece

        return self._rest.read(size)


class Sha256Stream(ByteStream):
    """Тройник: считает sha256 всего, что прошло через read, чтобы вызывающий
    получил хэш файла тем же проходом, которым ридер его читал."""

    def __init__(self, source: ByteStream) -> None:
        self._source = source
        self._digest = hashlib.sha256()

    def read(self, size: int = -1, /) -> bytes:
        chunk = self._source.read(size)
        self._digest.update(chunk)

        return chunk

    def exhaust(self) -> None:
        """Дочитать остаток, если ридер взял не всё: хэш должен покрыть файл."""
        while self._source.read(Spool.CHUNK):
            pass

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class TextSearch:
    """Поиск подстроки в тексте страницы; сниппет — context знаков вокруг."""

    @classmethod
    def hits(
        cls, page: ParsedPage, query: str, *, case_sensitive: bool, context: int
    ) -> Iterator[Hit]:
        haystack = page.text
        needle = query
        if not case_sensitive:
            haystack = haystack.lower()
            needle = needle.lower()

        start = 0
        while (index := haystack.find(needle, start)) >= 0:
            yield Hit(
                page=page.number,
                offset=index,
                length=len(needle),
                snippet=cls.snippet(page.text, index, len(needle), context),
            )
            start = index + len(needle)

    @staticmethod
    def snippet(text: str, offset: int, length: int, context: int) -> str:
        low = max(0, offset - context)
        high = min(len(text), offset + length + context)

        return text[low:high]


class PagedDocument(Document):
    """База документов по форматам: вид константой, карта и поиск через
    страницы, единая упаковка ошибок библиотеки в DocumentError.

    Наследники — PdfDocument, DocxDocument и остальные в boba.doc.readers.
    """

    KIND: ClassVar[DocumentKind]

    @property
    def kind(self) -> DocumentKind:
        return self.KIND

    def outline(self) -> Sequence[PageInfo]:
        infos: list[PageInfo] = []
        for page in self.pages(PageWindow.whole()):
            infos.append(PageInfo(number=page.number, chars=len(page.text)))

        return tuple(infos)

    def search(
        self, query: str, window: PageWindow, *, case_sensitive: bool, context: int
    ) -> Iterator[Hit]:
        if not query:
            raise DocumentError(
                f"{self.KIND.value} document: search expects a non-empty query"
            )

        for page in self.pages(window):
            yield from TextSearch.hits(
                page, query, case_sensitive=case_sensitive, context=context
            )

    def failure(self, action: str, exc: Exception) -> DocumentError:
        """Ошибка библиотеки формата в ошибке слоя, с действием и причиной."""
        return DocumentError(
            f"{self.KIND.value} document: {action} failed: {type(exc).__name__}: {exc}"
        )

    @classmethod
    def open_failure(cls, exc: Exception) -> DocumentError:
        return DocumentError(
            f"{cls.KIND.value} document: opening failed: {type(exc).__name__}: {exc}"
        )

    @abstractmethod
    def page_count(self) -> int: ...

    @abstractmethod
    def pages(self, window: PageWindow) -> Iterator[ParsedPage]: ...

    @abstractmethod
    def close(self) -> None: ...
