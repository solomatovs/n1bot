"""Helper-фрагменты для tool-конфигов: per-параметр overrides.

Каждый Tool имеет свою `ConfigSection` (`ext.<ext>.tools.<tool>`) с DTO,
содержащим:
  * `description: str` — описание tool'а (Default = canonical из кода).
  * `params: Mapping[str, ParamOverlay]` — оверрайды описаний параметров.
  * runtime-настройки (опционально).

Этот модуль даёт три примитива, которые повторно используются в tool-секциях:
  * `ParamOverlay` — DTO одного параметра.
  * `params_field()` — готовое `CollectionField(KeyedShape(ObjectItem(...)))`,
    которое материализует `params` через стандартный механизм
    `boba.declaration`.
  * `param_desc(params, name, default)` — read-helper: вернуть override из
    overlay или fallback на default из кода tool'а.

Использование в Tool.definition():

    return ObjectSchema(
        description=self._cfg.description,
        fields=[
            FieldSpec(name="path",
                      description=param_desc(self._cfg.params, "path", "Путь к файлу."),
                      ...),
        ],
    )
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from boba.coercion import ChainCoercer, Default, ParseString
from boba.declaration import (
    CollectionField,
    FieldSpec,
    KeyedShape,
    ObjectItem,
    ObjectSchema,
)

__all__ = [
    "ParamOverlay",
    "param_desc",
    "params_field",
]


@dataclass(frozen=True)
class ParamOverlay:
    """DTO одного параметра: пока только описание (расширяемо)."""

    description: str = ""


_PARAM_OVERLAY_SCHEMA: ObjectSchema[ParamOverlay] = ObjectSchema(
    description=(
        "Overlay одного параметра tool'а; пустое description — "
        "оставить дефолт из кода tool'а."
    ),
    fields=[
        FieldSpec(
            name="description",
            coercer=ChainCoercer(Default(""), ParseString()),
            description=(
                "Override описания параметра; пусто — оставить из кода."
            ),
        ),
    ],
    factory=ParamOverlay,
)


def param_desc(
    params: Mapping[str, ParamOverlay],
    name: str,
    default: str,
) -> str:
    """Описание параметра tool'а: override из overlay или дефолт из кода."""
    overlay = params.get(name)
    if overlay is None or not overlay.description:
        return default
    return overlay.description


def params_field(name: str = "params") -> CollectionField[str, Any, Any]:
    """Готовое поле `params: Mapping[str, ParamOverlay]` для tool-секции.

    Использование:

        class CatToolSection(ConfigSection[CatToolConfig]):
            schema = ObjectSchema(
                fields=[
                    FieldSpec("description", ...),
                    params_field(),  # ← params: dict[str, ParamOverlay]
                    FieldSpec("max_lines", ...),
                ],
                factory=CatToolConfig,
            )

    TOML:

        [ext.files.tools.cat.params.path]
        description = "..."
    """
    overlay_schema: ObjectSchema[Any] = _PARAM_OVERLAY_SCHEMA
    return CollectionField(
        name=name,
        reader=ObjectItem(overlay_schema),
        shape=KeyedShape(),
        description=(
            "Overrides per-параметр: ключ — имя параметра, значение — "
            "ParamOverlay (description, ...)."
        ),
    )
