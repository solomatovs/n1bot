"""Контракт LocaleRetry: ретраи только на ошибке конвертации LibreOffice."""

from __future__ import annotations

import os
from typing import Any

import pytest
from liteparse.types import ParseError

from boba.liteparse.engine import LocaleRetry


class ParserStub:
    """Скриптованный parser: отдаёт исходы по очереди и пишет виденный LC_ALL."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.locales: list[str | None] = []

    def parse(self, path: str) -> Any:
        self.locales.append(os.environ.get("LC_ALL"))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def marker_error() -> ParseError:
    return ParseError(f"conversion error: {LocaleRetry.MARKER}")


class TestLocaleRetry:
    def test_success_needs_no_retry(self) -> None:
        parser = ParserStub(["parsed"])
        if LocaleRetry.parse(parser, "/workspace/a.docx") != "parsed":
            raise AssertionError('LocaleRetry.parse(parser, "/workspace/a.docx") == "…')
        if parser.locales != [None]:
            raise AssertionError("parser.locales == [None]")

    def test_foreign_error_type_is_not_caught(self) -> None:
        parser = ParserStub([ValueError("broken document")])
        with pytest.raises(ValueError, match="broken document"):
            LocaleRetry.parse(parser, "/workspace/a.docx")
        if parser.locales != [None]:
            raise AssertionError("parser.locales == [None]")

    def test_parse_error_with_other_message_is_raised(self) -> None:
        parser = ParserStub([ParseError("unsupported format")])
        with pytest.raises(ParseError, match="unsupported format"):
            LocaleRetry.parse(parser, "/workspace/a.docx")
        if parser.locales != [None]:
            raise AssertionError("parser.locales == [None]")

    def test_marker_error_retries_with_locales(self) -> None:
        parser = ParserStub([marker_error(), "parsed"])
        if LocaleRetry.parse(parser, "/workspace/a.docx") != "parsed":
            raise AssertionError('LocaleRetry.parse(parser, "/workspace/a.docx") == "…')
        if parser.locales != [None, LocaleRetry.LOCALES[0]]:
            raise AssertionError("parser.locales == [None, LocaleRetry.LOCALES[0]]")
        if "LC_ALL" in os.environ:
            raise AssertionError('"LC_ALL" not in os.environ')

    def test_native_runtime_error_retries_too(self) -> None:
        parser = ParserStub(
            [RuntimeError(f"conversion error: {LocaleRetry.MARKER}"), "parsed"]
        )
        if LocaleRetry.parse(parser, "/workspace/a.docx") != "parsed":
            raise AssertionError('LocaleRetry.parse(parser, "/workspace/a.docx") == "…')
        if parser.locales != [None, LocaleRetry.LOCALES[0]]:
            raise AssertionError("parser.locales == [None, LocaleRetry.LOCALES[0]]")

    def test_exhausted_locales_raise_first_error(self) -> None:
        first = marker_error()
        retries: list[Any] = []
        for _ in LocaleRetry.LOCALES:
            retries.append(marker_error())
        parser = ParserStub([first, *retries])
        with pytest.raises(ParseError) as failure:
            LocaleRetry.parse(parser, "/workspace/a.docx")
        if failure.value is not first:
            raise AssertionError("failure.value is first")
        if parser.locales != [None, *LocaleRetry.LOCALES]:
            raise AssertionError("parser.locales == [None, *LocaleRetry.LOCALES]")

    def test_foreign_error_during_retry_is_raised(self) -> None:
        parser = ParserStub([marker_error(), ValueError("disk gone")])
        with pytest.raises(ValueError, match="disk gone"):
            LocaleRetry.parse(parser, "/workspace/a.docx")
        if "LC_ALL" in os.environ:
            raise AssertionError('"LC_ALL" not in os.environ')

    def test_existing_lc_all_is_restored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LC_ALL", "C.UTF-8")
        parser = ParserStub([marker_error(), "parsed"])
        if LocaleRetry.parse(parser, "/workspace/a.docx") != "parsed":
            raise AssertionError('LocaleRetry.parse(parser, "/workspace/a.docx") == "…')
        if os.environ["LC_ALL"] != "C.UTF-8":
            raise AssertionError('os.environ["LC_ALL"] == "C.UTF-8"')
