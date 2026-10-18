"""VisualResult: сводка для LLM; ContentMime: MIME по имени и суффикс по MIME."""

from __future__ import annotations

from boba.toolkit.result import ContentMime, VisualResult


class TestRenderForLlm:
    def test_with_title(self) -> None:
        result = VisualResult(
            element="Mermaid", props={"spec": "erDiagram"}, title="Схема заказов"
        )
        if result.llm_view() != "[Mermaid rendered: Схема заказов]":
            raise AssertionError('result.llm_view() == "[Mermaid rendered: Схема…')

    def test_without_title(self) -> None:
        result = VisualResult(element="Mermaid", props={"spec": "erDiagram"})
        if result.llm_view() != "[Mermaid rendered]":
            raise AssertionError('result.llm_view() == "[Mermaid rendered]"')


class TestContentMime:
    """MIME файла по имени и суффикс по MIME: своя таблица, потом stdlib."""

    def test_names_unknown_to_stdlib(self) -> None:
        expected = {
            "orders.mmd": "text/vnd.mermaid",
            "notes.MD": "text/markdown",
            "run.log": "text/plain",
            "shot.webp": "image/webp",
            "voice.ogg": "audio/ogg",
            "report.PDF": "application/pdf",
            "data.bin": "application/octet-stream",
        }
        for name, mime in expected.items():
            if ContentMime.of_name(name) != mime:
                raise AssertionError(f"{name}: {ContentMime.of_name(name)} == {mime}")

    def test_suffix_round_trip(self) -> None:
        """Файл, названный по MIME блока, опознаётся по имени тем же MIME."""
        for mime in ("audio/ogg", "audio/wav", "image/webp", "image/png"):
            suffix = ContentMime.suffix_of(mime)
            if ContentMime.of_name(f"a{suffix}") != mime:
                raise AssertionError(f"{mime}: suffix {suffix!r} does not round-trip")

        if ContentMime.suffix_of("application/x-unknown-thing") != "":
            raise AssertionError("unknown MIME has no suffix")
