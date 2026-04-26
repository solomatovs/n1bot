"""Value-objects, которые KB отдаёт tools (и через них агенту).

Tools затем сериализуют их в JSON для ``ToolResult.content``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionInfo:
    """Описание одной коллекции для ``kb_list_collections``.

    ``description`` — то, что оператор положил в ``metadata["description"]``
    при создании; пусто, если оператор не задал. Остальные поля
    metadata в этой версии в API не пробрасываются — только description,
    чтобы агенту не показывать служебные ключи.
    """

    name: str
    description: str


@dataclass(frozen=True)
class SearchHit:
    """Один результат ``kb_search``.

    ``distance`` — chromadb-distance (меньше = ближе). ``snippet`` —
    урезанная до ``ChromaExtConfig.snippet_chars`` копия документа,
    чтобы не раздувать контекст агента.
    """

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
