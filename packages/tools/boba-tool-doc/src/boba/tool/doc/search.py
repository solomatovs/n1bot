"""Tool: поиск фразы в документе с координатами совпадений (liteparse).

Bbox-merge (фраза может идти через несколько фрагментов) делает нативный
liteparse._liteparse.search_items — это «готовый модуль» движка. Берём
именно приватный нативный символ, потому что публичный liteparse.search_items
в 2.0.x сломан: он передаёт dataclass-TextItem (вывод публичного parse())
в нативную функцию, ждущую PyTextItem, и падает с TypeError. Поэтому
парсим нативным DocEngine.parse_native (отдаёт нативные items) и зовём
нативный search_items напрямую.

КОГДА АПСТРИМ ПОЧИНИТ публичный search_items — заменить импорт на
from liteparse import search_items, парсить обычным DocEngine.parse
и удалить parse_native/private-импорт.

Контекст-сниппет нативный модуль не даёт (возвращает только сам матч + bbox),
поэтому его собираем сами из page.text.
"""

from __future__ import annotations

from typing import Annotated, Any

from liteparse._liteparse import search_items as _native_search_items
from pydantic import Field

from boba.tool.doc._engine import DocEngine
from boba.tool.doc.config import DocPluginConfig
from boba.tools import FromConfig, FromDI, Scope, tool
from boba.tools.domain import TableResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["search_document"]


class _Search:
    """Поиск через нативный search_items; контекст-сниппет — из page.text."""

    @staticmethod
    def run(
        native_result: Any,
        query: str,
        *,
        context: int,
        max_matches: int,
    ) -> list[dict[str, Any]]:
        """Найти совпадения query на всех страницах; вернуть строки таблицы."""
        needle = query.casefold()
        rows: list[dict[str, Any]] = []
        for page in native_result.pages:
            hits = _native_search_items(page.text_items, query, case_sensitive=False)
            if not hits:
                continue
            hay = page.text.casefold()
            cursor = 0
            for hit in hits:
                idx = hay.find(needle, cursor)
                if idx == -1:
                    snippet = hit.text
                else:
                    snippet = _Search._snippet(
                        page.text, idx, idx + len(query), context,
                    )
                    cursor = idx + len(query)
                rows.append(
                    {
                        "page": page.page_num,
                        "x": round(hit.x, 1),
                        "y": round(hit.y, 1),
                        "width": round(hit.width, 1),
                        "height": round(hit.height, 1),
                        "snippet": snippet,
                    },
                )
                if len(rows) >= max_matches:
                    return rows
        return rows

    @staticmethod
    def _snippet(text: str, lo: int, hi: int, context: int) -> str:
        """Вырезать совпадение с контекстом по context символов с каждой стороны."""
        begin = max(0, lo - context)
        fin = min(len(text), hi + context)
        prefix = "…" if begin > 0 else ""
        suffix = "…" if fin < len(text) else ""
        return f"{prefix}{text[begin:fin]}{suffix}"


@tool
def search_document(
    path: Annotated[
        str,
        Field(min_length=1, description="Путь к документу в workspace."),
    ],
    query: Annotated[
        str,
        Field(min_length=1, description="Искомая фраза (регистронезависимо)."),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    cfg: Annotated[DocPluginConfig, FromConfig()],
) -> TableResult:
    """Найти фразу в документе; вернуть совпадения со страницей, bbox и сниппетом.

    Колонки: page, x/y/width/height (координаты совпадения на странице),
    snippet (контекст). Поиск регистронезависимый; число совпадений ограничено
    search_max_matches.
    """
    data = DocEngine.read_bytes(shell, path)
    native = DocEngine.parse_native(cfg, data, path)

    rows = _Search.run(
        native,
        query,
        context=cfg.search_context_chars,
        max_matches=cfg.search_max_matches,
    )
    capped = len(rows) >= cfg.search_max_matches
    note = f"{path}: совпадений {len(rows)}" + (" (лимит)" if capped else "")
    return TableResult(
        rows=rows,
        note=note,
        metadata={"path": path, "query": query},
    )
