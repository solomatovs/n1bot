"""UI-overrides для ``.chainlit/config.toml``.

Chainlit читает UI/features/project-настройки только из TOML —
env-переменные для этих секций фреймворк не смотрит. Мы декларируем
эти оверрайды как :class:`ChainlitUiOverrideSection`, при сборке
бандла читаем значения из единой цепочки источников и затем рендерим
.chainlit/config.toml через :class:`UIOverrideTomlConverter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from boba.domain.core.config import (
    ConfigSection,
    FieldSpec,
    ObjectSchema,
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


def _ui_override_factory(**raw: Any) -> UIOverride:
    """Маппинг operator-facing имён полей (UI-/FILE_UPLOAD_-префиксы)
    в DTO-имена. Имена в env (``BOBA_CHAINLIT_UI_NAME``,
    ``BOBA_CHAINLIT_FILE_UPLOAD_MAX_MB``, ...) намеренно отличаются от
    DTO-полей (``name``, ``upload_max_size_mb``, ...) — оператору они
    яснее в плоском namespace ``[chainlit]``.
    """
    return UIOverride(
        name=raw.get("ui_name"),
        enable_telemetry=raw.get("enable_telemetry"),
        upload_max_size_mb=raw.get("file_upload_max_mb"),
        upload_max_files=raw.get("file_upload_max_files"),
        upload_accept=raw.get("file_upload_accept"),
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

    schema: ClassVar[ObjectSchema[UIOverride]] = ObjectSchema(
        description="UI-overrides для .chainlit/config.toml: title, "
        "telemetry, лимиты загрузки файлов.",
        fields=[
            FieldSpec(
                name="ui_name",
                converter=Nullable(ParseString()),
                description="Заголовок чата в UI. "
                "Если не задано — chainlit-дефолт.",
            ),
            FieldSpec(
                name="enable_telemetry",
                converter=Nullable(ParseBool()),
                description="Опт-аут chainlit-телеметрии. "
                "None — не трогать дефолт.",
            ),
            FieldSpec(
                name="file_upload_max_mb",
                converter=Nullable(ParseInt()),
                description="Лимит размера загружаемого файла, MB.",
            ),
            FieldSpec(
                name="file_upload_max_files",
                converter=Nullable(ParseInt()),
                description="Максимум файлов в одном сообщении.",
            ),
            FieldSpec(
                name="file_upload_accept",
                converter=Nullable(ParseCsvList()),
                description="MIME-типы/расширения, разрешённые "
                "к загрузке (CSV).",
            ),
        ],
        factory=_ui_override_factory,
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
