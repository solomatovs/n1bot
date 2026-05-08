"""Рендер UI-полей ChainlitConfig в .chainlit/config.toml."""

from __future__ import annotations

from typing import ClassVar

from boba.patterns import (
    AllMatchesDispatcher,
    Converter,
    Specification,
)
from boba.web.chainlit.config import ChainlitConfig

__all__ = ["UIOverrideTomlConverter"]


def _has_upload(cfg: ChainlitConfig) -> bool:
    return any(
        v is not None
        for v in (cfg.upload_max_size_mb, cfg.upload_max_files, cfg.upload_accept)
    )


class _TelemetryIsSet(Specification[ChainlitConfig]):
    def check(self, candidate: ChainlitConfig) -> bool:
        return candidate.enable_telemetry is not None


class _HasUpload(Specification[ChainlitConfig]):
    def check(self, candidate: ChainlitConfig) -> bool:
        return _has_upload(candidate)


class _UiNameIsSet(Specification[ChainlitConfig]):
    def check(self, candidate: ChainlitConfig) -> bool:
        return candidate.ui_name is not None


class _ProjectRenderer(Converter[ChainlitConfig, str]):
    def convert(self, value: ChainlitConfig) -> str:
        flag = "true" if value.enable_telemetry else "false"
        return "\n".join(["[project]", f"enable_telemetry = {flag}", ""])


class _UploadRenderer(Converter[ChainlitConfig, str]):
    def convert(self, value: ChainlitConfig) -> str:
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


class _UIRenderer(Converter[ChainlitConfig, str]):
    def convert(self, value: ChainlitConfig) -> str:
        return "\n".join(["[UI]", f'name = "{value.ui_name}"', ""])


# chainlit падает, если [meta] generated_by <= "0.3.0" лексикографически.
_META = "\n".join(["[meta]", 'generated_by = "boba-chainlit"', ""])


class UIOverrideTomlConverter(Converter[ChainlitConfig, str]):
    """ChainlitConfig → TOML-строка для .chainlit/config.toml; пусто = не писать."""

    _ROUTES: ClassVar[
        list[tuple[Specification[ChainlitConfig], Converter[ChainlitConfig, str]]]
    ] = [
        (_TelemetryIsSet(), _ProjectRenderer()),
        (_HasUpload(), _UploadRenderer()),
        (_UiNameIsSet(), _UIRenderer()),
    ]

    def __init__(self) -> None:
        self._dispatch = AllMatchesDispatcher[ChainlitConfig, str](
            [(spec, renderer.convert) for spec, renderer in self._ROUTES]
        )

    def convert(self, value: ChainlitConfig) -> str:
        sections = list(self._dispatch(value))
        if not sections:
            return ""
        sections.append(_META)
        return "\n".join(sections)
