"""Grep-выдача на реальном тексте: нумерация, склейка групп, сводка."""

from __future__ import annotations

from boba.text.grep import GrepLimits, GrepReport, TextGrep

PAGE = "\n".join(f"line {number} общие сведения" for number in range(1, 40))


def _report(pattern: str, *, context: int, limit: int = 100) -> GrepReport:
    compiled = TextGrep.compile_pattern(
        pattern, fixed_string=False, case_insensitive=False
    )
    limits = GrepLimits(context=context, limit=limit, clip_chars=200)

    return TextGrep.report(PAGE, compiled, limits, "url=https://example/wiki")


class TestGrepReportRender:
    """Показ совпадений в форме ripgrep."""

    def test_matches_are_numbered_lines(self) -> None:
        rendered = _report("line 10 ", context=0).render()

        if rendered != "    10: line 10 общие сведения":
            raise AssertionError(f"нумерованная строка, получено {rendered!r}")

    def test_context_lines_take_the_dash_marker(self) -> None:
        lines = _report("line 10 ", context=1).render().splitlines()

        expected = [
            "     9- line 9 общие сведения",
            "    10: line 10 общие сведения",
            "    11- line 11 общие сведения",
        ]
        if lines != expected:
            raise AssertionError(f"контекст помечен дефисом, получено {lines!r}")

    def test_distant_groups_are_split_by_separator(self) -> None:
        rendered = _report("line (10|33) ", context=0).render()

        expected = "    10: line 10 общие сведения\n--\n    33: line 33 общие сведения"
        if rendered != expected:
            raise AssertionError(f"группы разделены '--', получено {rendered!r}")

    def test_overlapping_groups_print_each_line_once(self) -> None:
        """Пересечение контекстов не размножает строки; совпадения держат ':'."""
        lines = _report("line 1[0-2] ", context=1).render().splitlines()

        expected = [
            "     9- line 9 общие сведения",
            "    10: line 10 общие сведения",
            "    11: line 11 общие сведения",
            "    12: line 12 общие сведения",
            "    13- line 13 общие сведения",
        ]
        if lines != expected:
            raise AssertionError(f"склейка пересечений, получено {lines!r}")

    def test_no_matches_render_empty_text(self) -> None:
        report = _report("совершенно ничего", context=1)

        if report.render() != "":
            raise AssertionError("без совпадений текст пуст")

        if report.note != "url=https://example/wiki: no matches found":
            raise AssertionError(f"сводка об отсутствии, получено {report.note!r}")


class TestGrepReportNote:
    """Сводка выдачи: источник, число совпадений, признак усечения."""

    def test_note_counts_matches(self) -> None:
        report = _report("line 1[0-2] ", context=0)

        if report.note != "url=https://example/wiki; matches: 3":
            raise AssertionError(f"сводка со счётчиком, получено {report.note!r}")

    def test_limit_stops_the_output_and_marks_the_note(self) -> None:
        report = _report("line ", context=0, limit=2)

        if len(report.matches) != 2:
            raise AssertionError("лимит режет число совпадений")

        if "showing first 2 (more found)" not in report.note:
            raise AssertionError(f"сводка про усечение, получено {report.note!r}")


class TestGrepClipping:
    """Обрезка длинных строк: в выдачу не уезжает вся страница."""

    def test_content_is_clipped_to_the_limit(self) -> None:
        text = "заголовок\n" + "x" * 500
        compiled = TextGrep.compile_pattern(
            "x+", fixed_string=False, case_insensitive=False
        )
        limits = GrepLimits(context=1, limit=10, clip_chars=20)

        report = TextGrep.report(text, compiled, limits, "url=https://example/wiki")

        if len(report.matches[0].content) != 20:
            raise AssertionError("строка обрезана до clip_chars")
