"""
DispatchReader — Reader[T], выбирающий sub-Reader по MetadataKey[str].

Полезен когда один RequestSource/Transport отдаёт смешанный поток
документов разных форматов (например FsWalkRequestSource по папке
с .md, .html и .txt).
Каждый формат держит собственный Reader[T], а DispatchReader смотрит
на metadata-ключ (обычно FsKeys.SUFFIX или TransportKeys.CONTENT_TYPE) и делегирует.

Ключ ищется в RawDocument.metadata. Если ключ отсутствует или его
значение не покрыто routes — поведение задаётся on_unknown:

- "error" (default) — IncompatibleContentError: ошибка сборки pipeline,
  не transient. Используется когда unknown-формат в потоке — это баг
  конфигурации (например, FsWalkRequestSource неожиданно вернул .docx,
  для которого нет Reader'а).
- "skip" — yield ничего, документ молча игнорируется. Используется
  для multi-format потоков, где наличие неподдерживаемых форматов —
  это норма (например, Confluence-pipeline стримит HTML + произвольные
  attachment'ы, и мы хотим индексировать только HTML, остальное
  пропускать без шума).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal, TypeVar

from boba.indexing.errors import IncompatibleContentError
from boba.indexing.metadata import MetadataKey
from boba.indexing.raw_document import RawDocument
from boba.indexing.reader import Reader, ReaderId
from boba.indexing.sections import Section

__all__ = ["DispatchReader"]

T = TypeVar("T")


class DispatchReader(Reader[T]):
    """Reader[T], делегирующий sub-Reader'у по значению metadata-ключа.

    by — типизированный ключ, по которому различаем формат
    (FsKeys.SUFFIX, TransportKeys.CONTENT_TYPE и т.п.).
    routes — отображение «значение ключа -> Reader[T]».
    on_unknown — поведение при unknown-формате; см. module docstring.

    Pipeline остаётся честно один: один Indexer, один cleanup-scope,
    один stats-builder. Format-выбор инкапсулирован в одном узле.
    """

    def __init__(
        self,
        *,
        by: MetadataKey[str],
        routes: Mapping[str, Reader[T]],
        reader_id: ReaderId,
        on_unknown: Literal["error", "skip"] = "error",
    ) -> None:
        if not routes:
            msg = "DispatchReader: routes must be non-empty"
            raise ValueError(msg)
        self._by = by
        self._routes = dict(routes)
        self._reader_id = reader_id
        self._on_unknown = on_unknown

    def reader_id(self) -> ReaderId:
        return self._reader_id

    def read(self, raw: RawDocument) -> Iterable[Section[T]]:
        key_value = raw.metadata.get(self._by)
        if key_value is None:
            if self._on_unknown == "skip":
                return
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(raw.source_id),
                reason=(
                    f"metadata key {self._by.name!r} is missing; "
                    f"DispatchReader cannot pick a sub-reader"
                ),
            )

        inner = self._routes.get(key_value)
        if inner is None:
            if self._on_unknown == "skip":
                return
            supported = ", ".join(sorted(self._routes))
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(raw.source_id),
                reason=(
                    f"no sub-reader for {self._by.name}={key_value!r}; "
                    f"known: [{supported}]"
                ),
            )

        yield from inner.read(raw)
