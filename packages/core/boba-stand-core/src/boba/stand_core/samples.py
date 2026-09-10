"""Образцы документов для тестов: минимальный PDF, который читает парсер.

Прогоны doc-инструментов нуждаются в настоящем файле. Держать его в репозитории
двоичным неудобно, поэтому образец собирается из литерала: две страницы с
известным текстом, по нему проверяются и чтение, и поиск.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

__all__ = ["SamplePdf"]


class SamplePdf:
    """Двухстраничный PDF: «Alpha page one» и «Beta page two Alpha again»."""

    PAGES: ClassVar[int] = 2
    FIRST_PAGE: ClassVar[str] = "Alpha page one"
    SECOND_PAGE: ClassVar[str] = "Beta page two Alpha again"
    WORD: ClassVar[str] = "Alpha"
    """Слово, которое встречается на обеих страницах: цель поиска."""

    NAME: ClassVar[str] = "sample.pdf"

    BYTES: ClassVar[bytes] = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 50>>stream
BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R\
/Resources<</Font<</F1 5 0 R>>>>>>endobj
7 0 obj<</Length 60>>stream
BT /F1 20 Tf 20 200 Td (Beta page two Alpha again) Tj ET
endstream endobj
trailer<</Root 1 0 R/Size 8>>
%%EOF"""

    @classmethod
    def written(cls, directory: Path) -> Path:
        """Образец, записанный в каталог; путь готов для инструмента."""
        path = directory / cls.NAME
        path.write_bytes(cls.BYTES)

        return path
