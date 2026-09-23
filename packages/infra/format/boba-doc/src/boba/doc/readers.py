"""Документы по форматам поверх библиотек: pdf — pypdfium2, docx — python-docx,
xlsx — openpyxl, pptx — python-pptx, xls — xlrd, rtf — striprtf, картинки —
Pillow, текст — декодирование. Каждый получает поток без seek и сам решает,
как его буферизовать.

Ошибки:
DocumentError — библиотека не открыла файл или не прочитала страницу.
"""

from __future__ import annotations

import io
import math
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from enum import StrEnum
from typing import Any, ClassVar

import docx
import openpyxl
import pypdfium2 as pdfium
import xlrd
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph
from PIL import Image
from pptx import Presentation
from pptx.shapes.graphfrm import GraphicFrame
from pptx.shapes.group import GroupShape
from pptx.shapes.picture import Picture
from striprtf.striprtf import rtf_to_text

from boba.doc.document import (
    BoxedHit,
    ByteStream,
    DocumentError,
    DocumentKind,
    Hit,
    OcrEngine,
    PagedDocument,
    PageInfo,
    PageWindow,
    ParsedPage,
    SizedPageInfo,
    Spool,
    TextSearch,
)

__all__ = [
    "DocxDocument",
    "GridText",
    "ImageDocument",
    "PdfDocument",
    "PptxDocument",
    "RtfDocument",
    "TextDecoder",
    "TextDocument",
    "XlsDocument",
    "XlsxDocument",
]


class Glue(StrEnum):
    """Склейка текста: ячейки строки, строки страницы, блоки документа."""

    CELL = "\t"
    LINE = "\n"
    BLOCK = "\n"


class GridText:
    """Таблица или лист в текст: строка — ячейки через табуляцию, пустые
    ячейки и пустые строки опускаются."""

    @classmethod
    def render(cls, title: str, rows: Iterable[Sequence[object]]) -> str:
        lines = [title]
        lines.extend(cls._lines(rows))

        return Glue.LINE.join(lines)

    @classmethod
    def _lines(cls, rows: Iterable[Sequence[object]]) -> Iterator[str]:
        for row in rows:
            cells = list(cls._cells(row))
            if not cells:
                continue

            yield Glue.CELL.join(cells)

    @staticmethod
    def _cells(row: Sequence[object]) -> Iterator[str]:
        for value in row:
            if value is None:
                continue

            text = str(value).strip()
            if not text:
                continue

            yield text


class TextDecoder:
    """Байты в текст первой подошедшей кодировкой из списка конфига."""

    @staticmethod
    def decode(raw: bytes, encodings: Sequence[str], kind: DocumentKind) -> str:
        for encoding in encodings:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        tried = ", ".join(encodings)
        raise DocumentError(
            f"{kind.value} document: cannot decode {len(raw)} bytes "
            f"with any of: {tried}"
        )


class PictureText:
    """Текст с картинок внутри документа: страница pdf, абзац docx или слайд
    pptx отдают сюда свои растровые вставки, обратно приходят строки, которых в
    уже известном тексте нет. Мелкие иконки отсекает вызывающий по размеру
    вставки: меньше полудюйма по любой стороне — не картинка с текстом; при
    выключенном OCR картинки не декодируются вовсе."""

    MIN_SIDE_PT: ClassVar[float] = 36.0
    MIN_SIDE_EMU: ClassVar[int] = 457200

    def __init__(self, ocr: OcrEngine) -> None:
        self._ocr = ocr

    @property
    def enabled(self) -> bool:
        return self._ocr.enabled

    def lines(self, known: str, pictures: Iterable[Image.Image]) -> Sequence[str]:
        """Уникальные строки с картинок, отсутствующие в known."""
        return tuple(dict.fromkeys(self._fresh_lines(known, pictures)))

    def block(self, known: str, pictures: Iterable[Image.Image]) -> str:
        """Те же строки одним блоком; пусто, если картинки ничего не добавили."""
        return "\n".join(self.lines(known, pictures))

    def decode(self, blob: bytes) -> Image.Image:
        image = Image.open(io.BytesIO(blob))
        image.load()

        return image

    def _fresh_lines(
        self, known: str, pictures: Iterable[Image.Image]
    ) -> Iterator[str]:
        for picture in pictures:
            for line in self._ocr.recognize(picture).splitlines():
                stripped = line.strip()
                if not stripped:
                    continue

                if stripped in known:
                    continue

                yield stripped


class PdfDocument(PagedDocument):
    """PDF через pdfium: текстовый слой страницы, а если его нет — рендер
    страницы в картинку и OCR. У страницы со слоем распознаются ещё и
    встроенные картинки не меньше полудюйма по каждой стороне (скриншоты, схемы),
    их строки дописываются после слоя. Поиск по текстовому слою отдаёт
    координаты."""

    KIND: ClassVar[DocumentKind] = DocumentKind.PDF
    PICTURE_DEPTH: ClassVar[int] = 3
    RENDER_SCALE: ClassVar[int] = 2

    def __init__(
        self,
        spool: tempfile.SpooledTemporaryFile[bytes],
        pdf: pdfium.PdfDocument,
        ocr: OcrEngine,
    ) -> None:
        self._spool = spool
        self._pdf = pdf
        self._ocr = ocr
        self._pictures = PictureText(ocr)

    @classmethod
    def open(cls, stream: ByteStream, memory_limit: int, ocr: OcrEngine) -> PdfDocument:
        spool = Spool.fill(stream, memory_limit)
        try:
            pdf = pdfium.PdfDocument(spool)
        except Exception as exc:
            spool.close()
            raise cls.open_failure(exc) from exc

        return cls(spool, pdf, ocr)

    def page_count(self) -> int:
        return len(self._pdf)

    def outline(self) -> Sequence[PageInfo]:
        infos: list[PageInfo] = []
        for number in PageWindow.whole().numbers(self.page_count()):
            page = self._pdf[number - 1]
            try:
                width, height = page.get_size()
                text = self._text_of(page, number)
            finally:
                page.close()

            infos.append(
                SizedPageInfo(
                    number=number, chars=len(text), width=width, height=height
                )
            )

        return tuple(infos)

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(self.page_count()):
            page = self._pdf[number - 1]
            try:
                text = self._text_of(page, number)
            finally:
                page.close()

            yield ParsedPage(number=number, text=text)

    def search(
        self, query: str, window: PageWindow, *, case_sensitive: bool, context: int
    ) -> Iterator[Hit]:
        if not query:
            raise DocumentError("pdf document: search expects a non-empty query")

        for number in window.numbers(self.page_count()):
            page = self._pdf[number - 1]
            try:
                yield from self._search_page(
                    page, number, query, case_sensitive, context
                )
            finally:
                page.close()

    def close(self) -> None:
        self._pdf.close()
        self._spool.close()

    def _text_of(self, page: pdfium.PdfPage, number: int) -> str:
        try:
            layer = self._layer_text(page)
            if not layer.strip():
                whole = page.render(scale=self.RENDER_SCALE).to_pil()
                return self._ocr.recognize(whole)

            pictures = list(self._page_pictures(page))
        except DocumentError:
            raise
        except Exception as exc:
            raise self.failure(f"reading page {number}", exc) from exc

        extra = self._pictures.block(layer, pictures)
        if not extra:
            return layer

        return layer + Glue.BLOCK + extra

    def _page_pictures(self, page: pdfium.PdfPage) -> Iterator[Image.Image]:
        """Встроенные картинки страницы в их собственном разрешении; мелкие
        иконки и логотипы отсекаются размером на странице."""
        if not self._pictures.enabled:
            return

        for obj in page.get_objects(max_depth=self.PICTURE_DEPTH):
            if not isinstance(obj, pdfium.PdfImage):
                continue

            left, bottom, right, top = obj.get_bounds()
            if right - left < PictureText.MIN_SIDE_PT:
                continue

            if top - bottom < PictureText.MIN_SIDE_PT:
                continue

            yield obj.get_bitmap(render=True).to_pil()

    @staticmethod
    def _layer_text(page: pdfium.PdfPage) -> str:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            textpage.close()

    def _search_page(
        self,
        page: pdfium.PdfPage,
        number: int,
        query: str,
        case_sensitive: bool,
        context: int,
    ) -> Iterator[Hit]:
        """Текстовый слой ищется pdfium с координатами, распознанная страница —
        по тексту без координат."""
        try:
            textpage = page.get_textpage()
        except Exception as exc:
            raise self.failure(f"searching page {number}", exc) from exc

        try:
            text = textpage.get_text_bounded()
            if not text.strip():
                parsed = ParsedPage(number=number, text=self._text_of(page, number))
                yield from TextSearch.hits(
                    parsed, query, case_sensitive=case_sensitive, context=context
                )
                return

            yield from self._layer_hits(
                textpage, number, query, case_sensitive, context
            )
        except DocumentError:
            raise
        except Exception as exc:
            raise self.failure(f"searching page {number}", exc) from exc
        finally:
            textpage.close()

    @classmethod
    def _layer_hits(
        cls,
        textpage: pdfium.PdfTextPage,
        number: int,
        query: str,
        case_sensitive: bool,
        context: int,
    ) -> Iterator[BoxedHit]:
        total = textpage.count_chars()
        searcher = textpage.search(query, match_case=case_sensitive)
        while (found := searcher.get_next()) is not None:
            index, count = found
            low = max(0, index - context)
            high = min(total, index + count + context)
            snippet = textpage.get_text_range(low, high - low)
            left, bottom, right, top = cls._first_rect(textpage, index, count)
            yield BoxedHit(
                page=number,
                offset=index,
                length=count,
                snippet=snippet,
                x=left,
                y=bottom,
                width=right - left,
                height=top - bottom,
            )

    @staticmethod
    def _first_rect(
        textpage: pdfium.PdfTextPage, index: int, count: int
    ) -> tuple[float, float, float, float]:
        rects = textpage.count_rects(index, count)
        if rects == 0:
            return (0.0, 0.0, 0.0, 0.0)

        return textpage.get_rect(0)


class DocxDocument(PagedDocument):
    """Word через python-docx: абзацы и таблицы в порядке тела документа,
    картинки абзаца не меньше полудюйма идут в OCR отдельным блоком за ним.
    Страниц у docx нет, поэтому страница — пачка блоков фиксированного размера."""

    KIND: ClassVar[DocumentKind] = DocumentKind.DOCX
    BLOCKS_PER_PAGE: ClassVar[int] = 40
    DRAWING: ClassVar[str] = ".//w:drawing"
    EXTENT: ClassVar[str] = ".//wp:extent"
    BLIP: ClassVar[str] = ".//a:blip"

    def __init__(
        self, document: Any, blocks: Sequence[Paragraph | DocxTable], ocr: OcrEngine
    ) -> None:
        self._document = document
        self._blocks = blocks
        self._pictures = PictureText(ocr)

    @classmethod
    def open(
        cls, stream: ByteStream, memory_limit: int, ocr: OcrEngine
    ) -> DocxDocument:
        """python-docx читает пакет в память целиком, поэтому буфер потока
        закрывается сразу; текст и OCR картинок считаются при чтении окна."""
        with Spool.fill(stream, memory_limit) as spool:
            try:
                document = docx.Document(spool)
                blocks = tuple(cls._blocks_of(document))
            except Exception as exc:
                raise cls.open_failure(exc) from exc

        return cls(document, blocks, ocr)

    def page_count(self) -> int:
        return max(1, math.ceil(len(self._blocks) / self.BLOCKS_PER_PAGE))

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(self.page_count()):
            low = (number - 1) * self.BLOCKS_PER_PAGE
            high = low + self.BLOCKS_PER_PAGE
            try:
                texts = list(self._block_texts(self._blocks[low:high]))
            except DocumentError:
                raise
            except Exception as exc:
                raise self.failure(f"reading page {number}", exc) from exc

            yield ParsedPage(number=number, text=Glue.BLOCK.join(texts))

    def close(self) -> None:
        return

    @staticmethod
    def _blocks_of(document: Any) -> Iterator[Paragraph | DocxTable]:
        for item in document.iter_inner_content():
            if isinstance(item, (Paragraph, DocxTable)):
                yield item

    def _block_texts(self, blocks: Iterable[Paragraph | DocxTable]) -> Iterator[str]:
        for item in blocks:
            if isinstance(item, Paragraph):
                text = item.text.strip()
                if text:
                    yield text

                extra = self._pictures.block(text, self._paragraph_pictures(item))
                if extra:
                    yield extra

                continue

            yield GridText.render("", self._table_rows(item))

    def _paragraph_pictures(self, paragraph: Paragraph) -> Iterator[Image.Image]:
        """Растровые вставки абзаца не меньше полудюйма по каждой стороне."""
        if not self._pictures.enabled:
            return

        for drawing in paragraph._element.xpath(self.DRAWING):
            extents = drawing.xpath(self.EXTENT)
            blips = drawing.xpath(self.BLIP)
            if not extents:
                continue

            if not blips:
                continue

            if int(extents[0].get("cx")) < PictureText.MIN_SIDE_EMU:
                continue

            if int(extents[0].get("cy")) < PictureText.MIN_SIDE_EMU:
                continue

            part = self._document.part.related_parts[blips[0].get(qn("r:embed"))]
            yield self._pictures.decode(part.blob)

    @staticmethod
    def _table_rows(table: DocxTable) -> Iterator[Sequence[object]]:
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                cells.append(cell.text)

            yield cells


class XlsxDocument(PagedDocument):
    """Excel через openpyxl в режиме read_only: лист — страница, строки
    читаются лениво из zip и не держатся в памяти целиком."""

    KIND: ClassVar[DocumentKind] = DocumentKind.XLSX

    def __init__(
        self, spool: tempfile.SpooledTemporaryFile[bytes], workbook: Any
    ) -> None:
        self._spool = spool
        self._workbook = workbook

    @classmethod
    def open(cls, stream: ByteStream, memory_limit: int) -> XlsxDocument:
        spool = Spool.fill(stream, memory_limit)
        try:
            workbook = openpyxl.load_workbook(spool, read_only=True, data_only=True)
        except Exception as exc:
            spool.close()
            raise cls.open_failure(exc) from exc

        return cls(spool, workbook)

    def page_count(self) -> int:
        return len(self._workbook.worksheets)

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(self.page_count()):
            sheet = self._workbook.worksheets[number - 1]
            try:
                text = GridText.render(sheet.title, sheet.iter_rows(values_only=True))
            except Exception as exc:
                raise self.failure(f"reading sheet {number}", exc) from exc

            yield ParsedPage(number=number, text=text)

    def close(self) -> None:
        self._workbook.close()
        self._spool.close()


class XlsDocument(PagedDocument):
    """Excel 97-2003 через xlrd: библиотека берёт только байты целиком,
    лист — страница."""

    KIND: ClassVar[DocumentKind] = DocumentKind.XLS

    def __init__(self, book: Any) -> None:
        self._book = book

    @classmethod
    def open(cls, stream: ByteStream) -> XlsDocument:
        raw = Spool.drain(stream)
        try:
            book = xlrd.open_workbook(file_contents=raw)
        except Exception as exc:
            raise cls.open_failure(exc) from exc

        return cls(book)

    def page_count(self) -> int:
        return self._book.nsheets

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(self.page_count()):
            try:
                sheet = self._book.sheet_by_index(number - 1)
                text = GridText.render(sheet.name, self._rows(sheet))
            except Exception as exc:
                raise self.failure(f"reading sheet {number}", exc) from exc

            yield ParsedPage(number=number, text=text)

    def close(self) -> None:
        self._book.release_resources()

    @staticmethod
    def _rows(sheet: Any) -> Iterator[Sequence[object]]:
        for index in range(sheet.nrows):
            yield sheet.row_values(index)


class PptxDocument(PagedDocument):
    """PowerPoint через python-pptx: слайд — страница, текст рамок, таблиц,
    групп и заметок докладчика; картинки слайда не меньше полудюйма идут в OCR."""

    KIND: ClassVar[DocumentKind] = DocumentKind.PPTX

    def __init__(self, slides: Sequence[Any], ocr: OcrEngine) -> None:
        self._slides = slides
        self._pictures = PictureText(ocr)

    @classmethod
    def open(
        cls, stream: ByteStream, memory_limit: int, ocr: OcrEngine
    ) -> PptxDocument:
        """python-pptx читает пакет в память целиком, поэтому буфер потока
        закрывается сразу; текст и OCR картинок считаются при чтении окна."""
        with Spool.fill(stream, memory_limit) as spool:
            try:
                presentation = Presentation(spool)
                slides = tuple(presentation.slides)
            except Exception as exc:
                raise cls.open_failure(exc) from exc

        return cls(slides, ocr)

    def page_count(self) -> int:
        return max(1, len(self._slides))

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(self.page_count()):
            text = ""
            if number <= len(self._slides):
                text = self._slide_text(self._slides[number - 1], number)

            yield ParsedPage(number=number, text=text)

    def close(self) -> None:
        return

    def _slide_text(self, slide: Any, number: int) -> str:
        try:
            parts = list(self._shape_texts(slide.shapes))
            parts.extend(self._notes(slide))
            text = Glue.BLOCK.join(parts)

            images = self._slide_pictures(slide.shapes)
            extra = self._pictures.block(text, images)
        except DocumentError:
            raise
        except Exception as exc:
            raise self.failure(f"reading slide {number}", exc) from exc

        if not extra:
            return text

        return Glue.BLOCK.join([text, extra])

    def _slide_pictures(self, shapes: Iterable[Any]) -> Iterator[Image.Image]:
        """Картинки слайда, включая вложенные в группы, не меньше полудюйма."""
        if not self._pictures.enabled:
            return

        for shape in shapes:
            if isinstance(shape, GroupShape):
                yield from self._slide_pictures(shape.shapes)
                continue

            if not isinstance(shape, Picture):
                continue

            if int(shape.width) < PictureText.MIN_SIDE_EMU:
                continue

            if int(shape.height) < PictureText.MIN_SIDE_EMU:
                continue

            yield self._pictures.decode(shape.image.blob)

    @classmethod
    def _shape_texts(cls, shapes: Iterable[Any]) -> Iterator[str]:
        for shape in shapes:
            if isinstance(shape, GroupShape):
                yield from cls._shape_texts(shape.shapes)
                continue

            if isinstance(shape, GraphicFrame):
                if shape.has_table:
                    yield GridText.render("", cls._table_rows(shape))

                continue

            if not shape.has_text_frame:
                continue

            text = shape.text_frame.text.strip()
            if not text:
                continue

            yield text

    @staticmethod
    def _table_rows(frame: GraphicFrame) -> Iterator[Sequence[object]]:
        for row in frame.table.rows:
            cells: list[str] = []
            for cell in row.cells:
                cells.append(cell.text)

            yield cells

    @staticmethod
    def _notes(slide: Any) -> Iterator[str]:
        if not slide.has_notes_slide:
            return

        frame = slide.notes_slide.notes_text_frame
        if frame is None:
            return

        text = frame.text.strip()
        if not text:
            return

        yield text


class RtfDocument(PagedDocument):
    """RTF через striprtf: разметка снимается, кодовая страница берётся из
    заголовка документа. Одна страница."""

    KIND: ClassVar[DocumentKind] = DocumentKind.RTF

    def __init__(self, text: str) -> None:
        self._text = text

    @classmethod
    def open(cls, stream: ByteStream, encodings: Sequence[str]) -> RtfDocument:
        raw = Spool.drain(stream)
        markup = TextDecoder.decode(raw, encodings, cls.KIND)
        try:
            text = rtf_to_text(markup, errors="replace")
        except Exception as exc:
            raise cls.open_failure(exc) from exc

        return cls(text)

    def page_count(self) -> int:
        return 1

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(1):
            yield ParsedPage(number=number, text=self._text)

    def close(self) -> None:
        return


class TextDocument(PagedDocument):
    """Текстовый файл: декодирование кодировками из конфига, одна страница."""

    KIND: ClassVar[DocumentKind] = DocumentKind.TEXT

    def __init__(self, text: str) -> None:
        self._text = text

    @classmethod
    def open(cls, stream: ByteStream, encodings: Sequence[str]) -> TextDocument:
        raw = Spool.drain(stream)
        text = TextDecoder.decode(raw, encodings, cls.KIND)

        return cls(text)

    def page_count(self) -> int:
        return 1

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(1):
            yield ParsedPage(number=number, text=self._text)

    def close(self) -> None:
        return


class ImageDocument(PagedDocument):
    """Картинка через Pillow: кадр — страница (многостраничный tiff даёт
    несколько), текст каждой — OCR."""

    KIND: ClassVar[DocumentKind] = DocumentKind.IMAGE

    def __init__(
        self,
        spool: tempfile.SpooledTemporaryFile[bytes],
        image: Image.Image,
        ocr: OcrEngine,
    ) -> None:
        self._spool = spool
        self._image = image
        self._ocr = ocr

    @classmethod
    def open(
        cls, stream: ByteStream, memory_limit: int, ocr: OcrEngine
    ) -> ImageDocument:
        spool = Spool.fill(stream, memory_limit)
        try:
            image = Image.open(spool)
            image.load()
        except Exception as exc:
            spool.close()
            raise cls.open_failure(exc) from exc

        return cls(spool, image, ocr)

    def page_count(self) -> int:
        return getattr(self._image, "n_frames", 1)

    def outline(self) -> Sequence[PageInfo]:
        infos: list[PageInfo] = []
        for page in self.pages(PageWindow.whole()):
            infos.append(
                SizedPageInfo(
                    number=page.number,
                    chars=len(page.text),
                    width=float(self._image.width),
                    height=float(self._image.height),
                )
            )

        return tuple(infos)

    def pages(self, window: PageWindow) -> Iterator[ParsedPage]:
        for number in window.numbers(self.page_count()):
            try:
                self._image.seek(number - 1)
                frame = self._image.convert("RGB")
            except Exception as exc:
                raise self.failure(f"decoding frame {number}", exc) from exc

            yield ParsedPage(number=number, text=self._ocr.recognize(frame))

    def close(self) -> None:
        self._image.close()
        self._spool.close()
