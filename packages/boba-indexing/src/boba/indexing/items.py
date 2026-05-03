"""SourceItem: единица, которую Source отдаёт Reader'у."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["SourceItem"]


@dataclass(frozen=True)
class SourceItem:
    """Один документ от Source — единица для Reader.

    `source_id` — стабильный URI (`fs:/abs/path`, `confluence://space/page`),
    под которым Store хранит чанки этого документа. Используется для
    idempotent re-index: `Store.delete_by_source(source_id)` + upsert.

    `content_hint` — что Reader.accepts(item) проверяет: расширение без точки
    (`html`, `md`), MIME (`text/html`) или explicit-ключ (`confluence_html`).
    Договор по hint'ам — на уровне конкретных Source/Reader.

    `payload` — байты документа. Streaming больших файлов — отдельный кейс
    (потом, через payload-провайдеры).

    `content_hash` — для skip-if-same. Пустая строка = всегда переиндексировать.
    """

    source_id: str
    content_hint: str
    payload: bytes
    metadata: Mapping[str, str] = field(default_factory=dict)
    content_hash: str = ""
