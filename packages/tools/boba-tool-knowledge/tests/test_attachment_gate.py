"""Отбор вложений: маски из запроса, потолок конфига и картинки без OCR."""

from __future__ import annotations

import pytest

from boba.tool.kb.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
)

CONFIG_ALLOWED = AttachmentFilter.from_lists(
    media_types=("application/pdf", "image/png", "text/plain"),
    titles=(),
)


def _att(title: str, media_type: str) -> AttachmentInfo:
    return AttachmentInfo(
        id="1",
        title=title,
        media_type=media_type,
        file_size=10,
        download_path="/download/attachments/1/x",
        webui="/pages/1",
        version=1,
    )


class TestPatterns:
    """Маска с косой чертой — тип содержимого, без неё — имя файла."""

    def test_slash_goes_to_media_type(self) -> None:
        flt = AttachmentFilter.parse("application/pdf")
        if flt.media_type_patterns != ("application/pdf",):
            raise AssertionError("маска с косой чертой должна быть типом")
        if flt.title_patterns:
            raise AssertionError("в имена файлов ничего попасть не должно")

    def test_plain_goes_to_title(self) -> None:
        flt = AttachmentFilter.parse("*.pdf")
        if flt.title_patterns != ("*.pdf",):
            raise AssertionError("маска без косой черты — имя файла")

    @pytest.mark.parametrize(
        "raw",
        ["*.pdf, *.docx", "*.pdf *.docx", "*.pdf,*.docx", " *.pdf ,  *.docx "],
    )
    def test_separators(self, raw: str) -> None:
        flt = AttachmentFilter.parse(raw)
        if flt.title_patterns != ("*.pdf", "*.docx"):
            raise AssertionError(f"разделители разобраны неверно: {raw!r}")

    def test_empty_string_is_empty_filter(self) -> None:
        if not AttachmentFilter.parse("   ").is_empty():
            raise AssertionError("пустая строка — ни одной маски")


class TestGate:
    """Запрос выбирает внутри разрешённого, а не поверх него."""

    def test_empty_request_takes_nothing(self) -> None:
        gate = AttachmentGate.of(CONFIG_ALLOWED, "", ocr_enabled=True)

        if gate.wants_attachments():
            raise AssertionError("пустой запрос не просит вложений")
        if gate.verdict(_att("a.pdf", "application/pdf")) is not (
            AttachmentVerdict.NOT_REQUESTED
        ):
            raise AssertionError("без масок вложение брать не за чем")

    def test_requested_and_allowed_passes(self) -> None:
        gate = AttachmentGate.of(CONFIG_ALLOWED, "*.pdf", ocr_enabled=False)

        if gate.verdict(_att("руководство.pdf", "application/pdf")) is not (
            AttachmentVerdict.TAKE
        ):
            raise AssertionError("pdf разрешён конфигом и запрошен маской")

    def test_requested_but_not_allowed(self) -> None:
        gate = AttachmentGate.of(CONFIG_ALLOWED, "*.zip", ocr_enabled=False)

        if gate.verdict(_att("dump.zip", "application/zip")) is not (
            AttachmentVerdict.NOT_ALLOWED
        ):
            raise AssertionError("конфиг остаётся потолком для запроса")

    def test_star_takes_everything_allowed(self) -> None:
        gate = AttachmentGate.of(CONFIG_ALLOWED, "*", ocr_enabled=False)

        if gate.verdict(_att("notes.txt", "text/plain")) is not AttachmentVerdict.TAKE:
            raise AssertionError("'*' берёт всё, что разрешено")
        if gate.verdict(_att("dump.zip", "application/zip")) is not (
            AttachmentVerdict.NOT_ALLOWED
        ):
            raise AssertionError("'*' не расширяет список конфига")

    def test_image_without_ocr_is_skipped(self) -> None:
        gate = AttachmentGate.of(CONFIG_ALLOWED, "image/*", ocr_enabled=False)

        if gate.verdict(_att("scan.png", "image/png")) is not (
            AttachmentVerdict.IMAGE_WITHOUT_OCR
        ):
            raise AssertionError("картинку без OCR качать незачем")

    def test_image_with_ocr_is_taken(self) -> None:
        gate = AttachmentGate.of(CONFIG_ALLOWED, "image/*", ocr_enabled=True)

        if gate.verdict(_att("scan.png", "image/png")) is not AttachmentVerdict.TAKE:
            raise AssertionError("с OCR картинка идёт в разбор")
