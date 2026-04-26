"""UI-overrides для ``.chainlit/config.toml``.

Chainlit читает UI/features/project-настройки только из TOML —
env-переменные для этих секций фреймворк не смотрит. Мы декларируем
эти оверрайды как :class:`ChainlitUiOverrideSection`, при сборке
бандла читаем значения из единой цепочки источников и затем рендерим
.chainlit/config.toml через :class:`UIOverrideTomlConverter`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.domain.core.config import (
    ChainedConfigResolver,
    ConfigSection,
    FieldSpec,
)
from boba.domain.core.patterns import (
    AllMatchesDispatcher,
    Converter,
    Specification,
    StrId,
)
from boba.domain.core.validators import (
    Nullable,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

__all__ = [
    "ChainlitUiOverrideSection",
    "UIOverride",
    "UIOverrideTomlConverter",
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


class ChainlitUiOverrideSection(ConfigSection[UIOverride]):
    """Секция UI-оверрайдов chainlit. Регистрируется через entry-point
    ``boba.config_sections``; bootstrap читает её через
    ``bundle.section(ChainlitUiOverrideSection)`` и рендерит в TOML.

    Делит namespace ``("chainlit",)`` с :class:`ChainlitSection` —
    оба пишут в одну логическую секцию env/TOML, но дают разные DTO.
    """

    id: ClassVar[StrId] = StrId("chainlit_ui_override")
    namespace: ClassVar[tuple[str, ...]] = ("chainlit",)

    NAME: FieldSpec[str | None] = FieldSpec(
        name="ui_name",
        converter=Nullable(ParseString()),
        description="Заголовок чата в UI. Если не задано — chainlit-дефолт.",
    )
    ENABLE_TELEMETRY: FieldSpec[bool | None] = FieldSpec(
        name="enable_telemetry",
        converter=Nullable(ParseBool()),
        description="Опт-аут chainlit-телеметрии. None — не трогать дефолт.",
    )
    UPLOAD_MAX_MB: FieldSpec[int | None] = FieldSpec(
        name="file_upload_max_mb",
        converter=Nullable(ParseInt()),
        description="Лимит размера загружаемого файла, MB.",
    )
    UPLOAD_MAX_FILES: FieldSpec[int | None] = FieldSpec(
        name="file_upload_max_files",
        converter=Nullable(ParseInt()),
        description="Максимум файлов в одном сообщении.",
    )
    UPLOAD_ACCEPT: FieldSpec[list[str] | None] = FieldSpec(
        name="file_upload_accept",
        converter=Nullable(ParseCsvList()),
        description="MIME-типы/расширения, разрешённые к загрузке (CSV).",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (
        NAME,
        ENABLE_TELEMETRY,
        UPLOAD_MAX_MB,
        UPLOAD_MAX_FILES,
        UPLOAD_ACCEPT,
    )

    def build(self, resolver: ChainedConfigResolver) -> UIOverride:
        return UIOverride(
            name=self._read(self.NAME, resolver),
            enable_telemetry=self._read(self.ENABLE_TELEMETRY, resolver),
            upload_max_size_mb=self._read(self.UPLOAD_MAX_MB, resolver),
            upload_max_files=self._read(self.UPLOAD_MAX_FILES, resolver),
            upload_accept=self._read(self.UPLOAD_ACCEPT, resolver),
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
