"""Content-Disposition отдачи файла: ASCII в кавычках, не-ASCII через filename*."""

from __future__ import annotations

import pytest

from boba.canvas.transfer import ContentDisposition

pytestmark = pytest.mark.anyio


class TestContentDisposition:
    async def test_ascii_name_is_quoted(self) -> None:
        value = ContentDisposition.INLINE.header("report.pdf")

        assert value == 'inline; filename="report.pdf"'

    async def test_attachment_names_the_file(self) -> None:
        value = ContentDisposition.ATTACHMENT.header("page.html")

        assert value == 'attachment; filename="page.html"'

    async def test_quotes_and_backslashes_are_escaped(self) -> None:
        value = ContentDisposition.INLINE.header('we"ird\\.txt')

        assert value == 'inline; filename="we\\"ird\\\\.txt"'

    async def test_non_ascii_goes_to_rfc2231(self) -> None:
        value = ContentDisposition.INLINE.header("отчёт 1.pdf")

        assert (
            value == "inline; filename*=utf-8''%D0%BE%D1%82%D1%87%D1%91%D1%82%201.pdf"
        )
