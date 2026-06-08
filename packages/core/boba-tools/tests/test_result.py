"""Unit: PgCopyTextResult.iter_rows — парсинг COPY TEXT-формата."""

from __future__ import annotations

from boba.tools.domain import PgCopyTextResult


def test_iter_rows_header_and_data() -> None:
    res = PgCopyTextResult(text="a\tb\n1\t2\n3\t4\n")
    assert list(res.iter_rows()) == [["a", "b"], ["1", "2"], ["3", "4"]]


def test_iter_rows_unescapes_specials() -> None:
    """\\t / \\n / \\\\ внутри ячейки разворачиваются в реальные символы."""
    # ячейка содержит таб, перенос и обратный слеш (COPY TEXT-эскейпы)
    res = PgCopyTextResult(text="col\nx\\ty\\nz\\\\w\n")
    assert list(res.iter_rows()) == [["col"], ["x\ty\nz\\w"]]


def test_iter_rows_null_becomes_none() -> None:
    """\\N (NULL) -> None; пустая ячейка -> пустая строка."""
    res = PgCopyTextResult(text="a\tb\n\\N\t\n")
    assert list(res.iter_rows()) == [["a", "b"], [None, ""]]


def test_iter_rows_tab_delimiter_not_confused_with_escaped_tab() -> None:
    """Реальный таб — делимитер; \\t внутри ячейки — часть значения."""
    res = PgCopyTextResult(text="a\tb\nleft\\tinner\tright\n")
    assert list(res.iter_rows()) == [["a", "b"], ["left\tinner", "right"]]


def test_iter_rows_empty() -> None:
    assert list(PgCopyTextResult(text="").iter_rows()) == []


def test_iter_rows_header_only() -> None:
    res = PgCopyTextResult(text="a\tb\n")
    assert list(res.iter_rows()) == [["a", "b"]]
