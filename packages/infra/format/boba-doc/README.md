# boba-doc

Чтение документов по форматам окнами страниц. Пакет разделён по весу: базовая
установка даёт секции конфига (`boba.doc.config`) и контракт документа
(`boba.doc.document`), их импортирует приложение; ридеры, роутер и мост
(`boba.doc.readers`, `boba.doc.router`, `boba.doc.bridge`) живут за extra
`readers`, OCR (`boba.doc.ocr`) — за extra `ocr`. В песочницу плагина пакет
объявляет точку монтирования моделей `/var/cache/rapidocr` и apt-пакеты для
opencv (`libgl1`, `libglib2.0-0`).

Вход — поток байтов, у которого
есть только `read`: открытый файл, конец пипы, сокет через `makefile`. Ридер
формата читает документ в память целиком (`MemoryFile`): временных файлов на
диске не остаётся, а размер документа ограничивает только лимит памяти
процесса.

```python
router = DocumentRouter(DocConfig(text_encodings=["utf-8"]), ocr)
# из потока корутины: AsyncPipe.run(chunks, consume) — чанки в пипу, ридер в потоке

with router.open(stream, DocumentHint(media_type=..., filename=...)) as document:
    document.page_count()
    document.outline()
    document.pages(PageWindow(start=1, count=10))
    document.search("query", PageWindow.whole(), case_sensitive=False)
```

| Вид | Библиотека | Страница |
|---|---|---|
| pdf | pypdfium2 | страница; без текстового слоя — рендер и OCR; поиск с координатами |
| docx | python-docx | 40 блоков (абзацы и таблицы по порядку тела) |
| xlsx | openpyxl read_only | лист |
| xls | xlrd | лист |
| pptx | python-pptx | слайд с таблицами, группами и заметками |
| rtf | striprtf | одна |
| text | кодировки по порядку | одна |
| image | Pillow + OCR | кадр |

Вид определяется `Formats.detect`: media_type, потом суффикс имени, потом
первые байты файла. `Sha256Stream` считает хэш файла тем же проходом.

OCR — extra `ocr`: `RapidOcrEngine` на моделях PP-OCRv5 через rapidocr и
onnxruntime; файлы моделей (`OcrModel`) лежат в `models_dir` конфига, из сети
ничего не берётся. Секция `DocSection` несёт `ocr` — union по `provider`
(`off` | `rapidocr`), движок по ней собирает `OcrEngines.of`, а `for_call(ocr=...)`
даёт секцию под вызов: без OCR, если вызов его не просил, и `OcrUnavailableError`,
если просил при `provider = off`. Без OCR подставляется `DisabledOcr`, картинки и
сканы дают пустой текст. Модели в сборку кладёт `make fetch` (`RAPIDOCR_LANGS`).
