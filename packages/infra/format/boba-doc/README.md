# boba-doc

Чтение документов по форматам окнами страниц. Вход — поток байтов, у которого
есть только `read`: открытый файл, конец пипы, сокет через `makefile`. Как
буферизовать байты, решает ридер формата: текст читается с потока, zip-форматы
и pdf сливаются в спул (до `spool_memory_limit` в памяти, дальше безымянный
временный файл).

```python
router = DocumentRouter(DocConfig(spool_memory_limit=32 << 20, text_encodings=["utf-8"]), ocr)

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
ничего не берётся. Без OCR подставляется `DisabledOcr`, картинки и сканы дают
пустой текст.
