"""Рендер ``.chainlit/config.toml`` из env-оверрайдов.

Chainlit читает UI/features/project-настройки только из TOML —
env-переменные для этих секций фреймворк не смотрит.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.chainlit.config import chainlit_resolver
from boba.domain.core.config import (
    BoolConverter,
    ChainedConfigResolver,
    CsvListConverter,
    FieldSpec,
    IntConverter,
    StrConverter,
)
from boba.domain.core.patterns import (
    AllMatchesDispatcher,
    Converter,
    Specification,
)

__all__ = [
    "UIOverride",
    "UIOverrideTomlConverter",
    "read_ui_override",
]


@dataclass(frozen=True)
class UIOverride:
    name: str | None = None
    enable_telemetry: bool | None = None
    upload_max_size_mb: int | None = None
    upload_max_files: int | None = None
    upload_accept: list[str] | None = None

    def has_upload(self) -> bool:
        return any(
            v is not None
            for v in (
                self.upload_max_size_mb,
                self.upload_max_files,
                self.upload_accept,
            )
        )


UI_NAME = FieldSpec("CHAINLIT_UI_NAME", StrConverter(), None)
UI_ENABLE_TELEMETRY = FieldSpec("CHAINLIT_ENABLE_TELEMETRY", BoolConverter(), None)
UI_UPLOAD_MAX_MB = FieldSpec("CHAINLIT_FILE_UPLOAD_MAX_MB", IntConverter(), None)
UI_UPLOAD_MAX_FILES = FieldSpec("CHAINLIT_FILE_UPLOAD_MAX_FILES", IntConverter(), None)
UI_UPLOAD_ACCEPT = FieldSpec("CHAINLIT_FILE_UPLOAD_ACCEPT", CsvListConverter(), None)


def read_ui_override(resolver: ChainedConfigResolver | None = None) -> UIOverride:
    r = resolver or chainlit_resolver()
    return UIOverride(
        name=UI_NAME.read_opt(r),
        enable_telemetry=UI_ENABLE_TELEMETRY.read_opt(r),
        upload_max_size_mb=UI_UPLOAD_MAX_MB.read_opt(r),
        upload_max_files=UI_UPLOAD_MAX_FILES.read_opt(r),
        upload_accept=UI_UPLOAD_ACCEPT.read_opt(r),
    )


class _TelemetryIsSet(Specification[UIOverride]):
    def check(self, candidate: UIOverride) -> bool:
        return candidate.enable_telemetry is not None


class _HasUpload(Specification[UIOverride]):
    def check(self, candidate: UIOverride) -> bool:
        return candidate.has_upload()


class _NameIsSet(Specification[UIOverride]):
    def check(self, candidate: UIOverride) -> bool:
        return candidate.name is not None


class _ProjectRenderer(Converter[UIOverride, str]):
    def convert(self, value: UIOverride) -> str:
        flag = "true" if value.enable_telemetry else "false"
        return "\n".join(["[project]", f"enable_telemetry = {flag}", ""])


class _UploadRenderer(Converter[UIOverride, str]):
    def convert(self, value: UIOverride) -> str:
        # enabled=true обязателен: иначе chainlit скрывает кнопку загрузки.
        accept = value.upload_accept or ["*/*"]
        accept_toml = "[" + ", ".join(f'"{a}"' for a in accept) + "]"
        lines = ["[features.spontaneous_file_upload]", "enabled = true"]
        if value.upload_max_size_mb is not None:
            lines.append(f"max_size_mb = {value.upload_max_size_mb}")
        if value.upload_max_files is not None:
            lines.append(f"max_files = {value.upload_max_files}")
        lines.append(f"accept = {accept_toml}")
        lines.append("")
        return "\n".join(lines)


class _UIRenderer(Converter[UIOverride, str]):
    def convert(self, value: UIOverride) -> str:
        return "\n".join(["[UI]", f'name = "{value.name}"', ""])


# chainlit проверяет [meta] generated_by на старте и падает, если оно
# <= "0.3.0" (лексикографически). "boba-chainlit" проходит.
_META = "\n".join(["[meta]", 'generated_by = "boba-chainlit"', ""])


class UIOverrideTomlConverter(Converter[UIOverride, str]):
    _ROUTES: ClassVar[
        list[tuple[Specification[UIOverride], Converter[UIOverride, str]]]
    ] = [
        (_TelemetryIsSet(), _ProjectRenderer()),
        (_HasUpload(), _UploadRenderer()),
        (_NameIsSet(), _UIRenderer()),
    ]

    def __init__(self) -> None:
        self._dispatch = AllMatchesDispatcher[UIOverride, str](
            [(spec, renderer.convert) for spec, renderer in self._ROUTES]
        )

    def convert(self, value: UIOverride) -> str:
        sections = list(self._dispatch(value))
        if not sections:
            return ""
        sections.append(_META)
        return "\n".join(sections)
