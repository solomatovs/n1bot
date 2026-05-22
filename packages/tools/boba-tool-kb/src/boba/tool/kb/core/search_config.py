"""`SearchConfig` — конфиг секции `[tool.kb.search]`.

Закрепляет за `kb_search`/`vector_search` список коллекций, в которых
выполняется поиск. Разведено с ingest-секциями (`[tool.kb.files]`,
`[tool.kb.confluence_ingest]`), чтобы scope чтения настраивался
независимо от scope записи: оператор может, например, индексировать
в `kb_files` + `kb_confluence`, а поиск пустить только по `kb_files`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList

__all__ = ["SearchConfig"]


class SearchConfig(BobaFlatSettings):
    """Список коллекций, по которому ищут `kb_search` и `vector_search`.

    Config-секция: `[tool.kb.search]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search",
    )

    collections: StringList = Field(
        default_factory=lambda: ["kb_files", "kb_confluence"],
        description=(
            "Список коллекций (`collection` в `kb_chunks`), по объединению "
            "которых выполняют поиск `kb_search` и `vector_search`. "
            "SQL-уровень: `WHERE collection = ANY(%(collections)s)`. "
            'CSV в env (`"a,b"`) или TOML-array (`["a", "b"]`).'
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.collections:
            msg = "tool.kb.search.collections не должен быть пустым"
            raise ValueError(msg)
        for item in self.collections:
            if not item or not item.strip():
                msg = (
                    "tool.kb.search.collections содержит пустую строку — "
                    "каждая коллекция должна быть непустым именем"
                )
                raise ValueError(msg)
        return self
