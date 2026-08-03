"""Payload разбора: документы (liteparse), HTML (bs4/markdownify), графики.

Исполняется python'ом из sandbox-rootfs-doc, где кроме stdlib есть только
парсеры, поэтому пакетов boba здесь нет. Запрос приходит JSON'ом на stdin,
ответ уходит одной строкой `sandbox-result:{json}` в stdout; всё остальное в
stdout/stderr — свободный лог.

Один payload на оба вида разбора: rootfs у них общий, а инструменту так
нужна ровно одна точка входа в его секции [tool.<name>.sandbox].

Модуль операции импортируется только когда она вызвана: инструменту нужен
свой парсер, а не все сразу — иначе отсутствие markdownify в rootfs ломало бы
и чтение документов, которому он не нужен.
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any, ClassVar

MARKER = "sandbox-result:"


class ParsePayload:
    """Разбирает запрос, выбирает операцию и печатает результат."""

    @classmethod
    def main(cls, stdin: str) -> int:
        request = json.loads(stdin)
        answer = cls.dispatch(request)
        body = json.dumps(answer, ensure_ascii=False)
        sys.stdout.write(f"{MARKER}{body}\n")
        return 0

    DOCUMENT_OPS: ClassVar[frozenset[str]] = frozenset(
        {
            "read_document",
            "read_pages",
            "read_document_window",
            "document_outline",
            "search_document",
            "parse_bytes",
        }
    )
    PAGE_OPS: ClassVar[frozenset[str]] = frozenset(
        {"to_markdown", "plain_text", "confluence_sections"}
    )
    CHART_OPS: ClassVar[frozenset[str]] = frozenset({"validate_figure"})
    WEB_OPS: ClassVar[frozenset[str]] = frozenset({"web_fetch", "web_grep"})
    POSTGRES_OPS: ClassVar[frozenset[str]] = frozenset({"pg_query", "pg_copy"})
    KB_OPS: ClassVar[frozenset[str]] = frozenset(
        {"kb_vector_search", "kb_fts_search"}
    )
    CONFLUENCE_OPS: ClassVar[frozenset[str]] = frozenset(
        {
            "confluence_page",
            "confluence_grep",
            "confluence_search",
            "confluence_spaces",
            "confluence_attachment",
        }
    )

    ROUTES: ClassVar[tuple[tuple[frozenset[str], str, str], ...]] = (
        (DOCUMENT_OPS, "documents", "DocumentOps"),
        (PAGE_OPS, "pages", "PageOps"),
        (CHART_OPS, "charts", "ChartOps"),
        (WEB_OPS, "web", "WebOps"),
        (POSTGRES_OPS, "postgres", "PostgresOps"),
        (KB_OPS, "knowledge", "KbOps"),
        (CONFLUENCE_OPS, "confluence", "ConfluenceOps"),
    )
    """Операция -> модуль, который её умеет. Модуль импортируется по факту
    вызова: инструменту нужен свой парсер, а не все сразу."""

    @classmethod
    def dispatch(cls, request: dict[str, Any]) -> dict[str, Any]:
        op = request["op"]
        for ops, module_name, class_name in cls.ROUTES:
            if op not in ops:
                continue
            module = importlib.import_module(module_name)
            return getattr(module, class_name).dispatch(request)
        msg = f"unknown op: {op!r}"
        raise ValueError(msg)


if __name__ == "__main__":
    sys.exit(ParsePayload.main(sys.stdin.read()))
