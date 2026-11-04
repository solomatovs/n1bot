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
from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from PIL import Image

__all__ = [
    "BoxedHit",
    "ByteStream",
    "DisabledOcr",
    "Document",
    "DocumentError",
    "DocumentHint",
    "DocumentKind",
    "FormatSpec",
    "Formats",
    "Hit",
    "MemoryFile",
    "OcrEngine",
    "PageInfo",
    "PageWindow",
    "PagedDocument",
    "ParsedPage",
    "Prefixed",
    "Sha256Stream",
    "SizedPageInfo",
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


DocumentKind = str
"""Открытый идентификатор формата, который объявляет реализация ридера."""


@dataclass(frozen=True)
class DocumentHint:
    """Что известно о документе до байтов: media_type и имя файла.

    Оба поля могут быть пустыми — тогда вид определяется по первым байтам.
    """

    media_type: str = ""
    filename: str = ""


@dataclass(frozen=True)
class FormatSpec:
    """Признаки одного формата; регистрацию предоставляет реализация ридера."""

    kind: DocumentKind
    media_types: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    media_prefixes: tuple[str, ...] = ()
    recognize: Callable[[bytes], bool] | None = None


class Formats:
    """Определяет формат только по переданным регистрациям ридеров."""

    HEAD_SIZE: ClassVar[int] = 8192

    def __init__(self, formats: Iterable[FormatSpec]) -> None:
        self._formats = tuple(formats)
        self._media: dict[str, DocumentKind] = {}
        self._suffixes: dict[str, DocumentKind] = {}
        self._prefixes: dict[str, DocumentKind] = {}
        kinds: set[str] = set()
        for spec in self._formats:
            if not spec.kind:
                raise DocumentError("reader registration needs a non-empty kind")

            if spec.kind in kinds:
                raise DocumentError(f"reader kind {spec.kind!r} is registered twice")

            kinds.add(spec.kind)
            self._register(self._media, spec.media_types, spec.kind)
            self._register(self._suffixes, spec.suffixes, spec.kind)
            self._register(self._prefixes, spec.media_prefixes, spec.kind)

    def _register(
        self, table: dict[str, DocumentKind], names: Iterable[str], kind: DocumentKind
    ) -> None:
        for name in names:
            if name in table:
                raise DocumentError(
                    f"format key {name!r} is registered by both "
                    f"{table[name]!r} and {kind!r}"
                )

            table[name] = kind

    def media_types(self) -> tuple[str, ...]:
        return tuple(self._media)

    @staticmethod
    def normalize(media_type: str) -> str:
        return media_type.partition(";")[0].strip().lower()

    def of_media_type(self, media_type: str) -> DocumentKind | None:
        normalized = self.normalize(media_type)
        found = self._media.get(normalized)
        if found is not None:
            return found

        for prefix, kind in self._prefixes.items():
            if normalized.startswith(prefix):
                return kind

        return None

    def of_filename(self, filename: str) -> DocumentKind | None:
        _, separator, suffix = filename.rpartition(".")
        if not separator:
            return None

        return self._suffixes.get(suffix.lower())

    def of_hint(self, hint: DocumentHint) -> DocumentKind | None:
        kind = self.of_media_type(hint.media_type)
        if kind is not None:
            return kind

        return self.of_filename(hint.filename)

    def detect(self, hint: DocumentHint, head: bytes) -> DocumentKind | None:
        kind = self.of_hint(hint)
        if kind is not None:
            return kind

        return self.sniff(head)

    def sniff(self, head: bytes) -> DocumentKind | None:
        for spec in self._formats:
            if spec.recognize is None:
                continue

            try:
                recognized = spec.recognize(head)
            except Exception as exc:
                raise DocumentError(
                    f"detecting format {spec.kind!r} failed: {exc}"
                ) from exc

            if recognized:
                return spec.kind

        return None


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


class MemoryFile:
    """Документ целиком в памяти: ридерам форматов нужен произвольный доступ
    по файлу, а документ обязан помещаться в память процесса — потолок задаёт
    лимит памяти песочницы, временных файлов на диске не остаётся."""

    CHUNK: ClassVar[int] = 1 << 20

    @classmethod
    def file(cls, stream: ByteStream) -> io.BytesIO:
        """Остаток потока файловым объектом с seek: для zip, pdf и картинок."""
        buffer = io.BytesIO()
        while chunk := stream.read(cls.CHUNK):
            buffer.write(chunk)

        buffer.seek(0)

        return buffer

    @classmethod
    def data(cls, stream: ByteStream) -> bytes:
        """Остаток потока байтами: для форматов, которые разбирают bytes."""
        return cls.file(stream).getvalue()


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
        while self._source.read(MemoryFile.CHUNK):
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
                f"{self.KIND} document: search expects a non-empty query"
            )

        for page in self.pages(window):
            yield from TextSearch.hits(
                page, query, case_sensitive=case_sensitive, context=context
            )

    def failure(self, action: str, exc: Exception) -> DocumentError:
        """Ошибка библиотеки формата в ошибке слоя, с действием и причиной."""
        return DocumentError(
            f"{self.KIND} document: {action} failed: {type(exc).__name__}: {exc}"
        )

    @classmethod
    def open_failure(cls, exc: Exception) -> DocumentError:
        return DocumentError(
            f"{cls.KIND} document: opening failed: {type(exc).__name__}: {exc}"
        )

    @abstractmethod
    def page_count(self) -> int: ...

    @abstractmethod
    def pages(self, window: PageWindow) -> Iterator[ParsedPage]: ...

    @abstractmethod
    def close(self) -> None: ...
