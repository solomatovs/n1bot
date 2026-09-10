"""Отбор вложений: allowlist конфига масками, флаг запроса и картинки без OCR."""

from __future__ import annotations

from boba.tool.kb.confluence.models import (
    AttachmentFilter,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
)

CONFIG_ALLOWED = AttachmentFilter.of_masks(
    ("application/pdf", "image/png", "text/plain", "report-*.docx"),
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
        when="2026-01-01T00:00:00.000Z",
    )


class TestMasks:
    """Маска с косой чертой — тип содержимого, без неё — имя файла."""

    def test_slash_goes_to_media_type(self) -> None:
        flt = AttachmentFilter.of_masks(("application/pdf",))
        if flt.media_type_patterns != ("application/pdf",):
            raise AssertionError("маска с косой чертой должна быть типом")
        if flt.title_patterns:
            raise AssertionError("в имена файлов ничего попасть не должно")

    def test_plain_goes_to_title(self) -> None:
        flt = AttachmentFilter.of_masks(("*.pdf",))
        if flt.title_patterns != ("*.pdf",):
            raise AssertionError("маска без косой черты — имя файла")

    def test_blank_masks_are_dropped(self) -> None:
        flt = AttachmentFilter.of_masks(("  ", "*.pdf", ""))
        if flt.title_patterns != ("*.pdf",):
            raise AssertionError("пустые маски не считаются")

    def test_no_masks_is_passthrough(self) -> None:
        if not AttachmentFilter.of_masks(()).is_passthrough():
            raise AssertionError("без масок разрешено всё")


class TestGate:
    """Запрос включает вложения, конфиг остаётся потолком."""

    def test_not_requested_takes_nothing(self) -> None:
        gate = AttachmentGate(allowed=CONFIG_ALLOWED, requested=False, ocr=True)

        if gate.verdict(_att("a.pdf", "application/pdf")) is not (
            AttachmentVerdict.NOT_REQUESTED
        ):
            raise AssertionError("без запроса вложение брать не за чем")

    def test_requested_and_allowed_passes(self) -> None:
        gate = AttachmentGate(allowed=CONFIG_ALLOWED, requested=True, ocr=False)

        if gate.verdict(_att("руководство.pdf", "application/pdf")) is not (
            AttachmentVerdict.TAKE
        ):
            raise AssertionError("pdf разрешён конфигом и запрошен")

    def test_title_mask_passes(self) -> None:
        gate = AttachmentGate(allowed=CONFIG_ALLOWED, requested=True, ocr=False)

        if gate.verdict(_att("report-q1.docx", "application/zip")) is not (
            AttachmentVerdict.TAKE
        ):
            raise AssertionError("маска по имени пропускает независимо от типа")

    def test_requested_but_not_allowed(self) -> None:
        gate = AttachmentGate(allowed=CONFIG_ALLOWED, requested=True, ocr=False)

        if gate.verdict(_att("dump.zip", "application/zip")) is not (
            AttachmentVerdict.NOT_ALLOWED
        ):
            raise AssertionError("конфиг остаётся потолком для запроса")

    def test_image_without_ocr_is_skipped(self) -> None:
        gate = AttachmentGate(allowed=CONFIG_ALLOWED, requested=True, ocr=False)

        if gate.verdict(_att("scan.png", "image/png")) is not (
            AttachmentVerdict.IMAGE_WITHOUT_OCR
        ):
            raise AssertionError("картинку без OCR качать незачем")

    def test_image_with_ocr_is_taken(self) -> None:
        gate = AttachmentGate(allowed=CONFIG_ALLOWED, requested=True, ocr=True)

        if gate.verdict(_att("scan.png", "image/png")) is not AttachmentVerdict.TAKE:
            raise AssertionError("с OCR картинка идёт в разбор")
