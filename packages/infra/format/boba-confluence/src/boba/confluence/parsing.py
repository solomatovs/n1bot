"""Разбор REST-JSON Confluence.

- JsonNode              — узел ответа: поля схемы по пути ключей, терпимо к
  расхождениям версий Confluence (нет ключа, не тот тип — пустое значение).
- ConfluenceJsonDecoder — REST-JSON страницы -> HTML-handle + хэш тела +
  расширенная metadata (title/version/space/ancestors).
- BodyHasher            — sha256 тел для реестра источников и хэшей индекса.

Саму HTML-разметку здесь никто не разбирает: она живёт в boba.confluence.html.

Ошибки:
ConfluencePayloadError — REST-ответ не разобран в модель страницы.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import ValidationError

from boba.confluence.models import (
    ConfluenceContent,
    ConfluenceKeys,
    ConfluencePayloadError,
    HttpKeys,
)
from boba.indexing import (
    ChunkStream,
    Decoder,
    DecoderId,
    RawDocument,
    ReaderKeys,
    TransportKeys,
)
from boba.transport.http.profile import HttpConnection

__all__ = [
    "BodyEncoding",
    "BodyHasher",
    "ConfluenceJsonDecoder",
    "JsonNode",
    "RunningDigest",
]


class JsonNode:
    """Узел REST-JSON Confluence: чтение полей по пути ключей.

    Создаётся над любым значением ответа; отсутствующий ключ, не-dict по пути
    или нечисловое значение дают пустой результат, а не исключение — так
    один разбор переживает расхождения версий Confluence.
    """

    def __init__(self, data: Any) -> None:
        self._data = data

    @property
    def raw(self) -> Any:
        return self._data

    def dict(self, *path: str) -> dict[str, Any]:
        data = self._data
        for key in path:
            if not isinstance(data, dict):
                return {}

            data = data.get(key)

        if not isinstance(data, dict):
            return {}

        return data

    def str(self, *path: str) -> str:
        value = self._value(*path)
        if value is None:
            return ""

        return str(value)

    def int(self, *path: str, default: int = 0) -> int:
        value = self._value(*path)
        if value is None:
            return default

        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def list(self, *path: str) -> list[Any]:
        value = self._value(*path)
        if not isinstance(value, list):
            return []

        return value

    def results(self) -> list[dict[str, Any]]:
        """Элементы списка ответа: results либо page.results у поиска."""
        items = self.list("results")
        if not items:
            items = self.list("page", "results")

        found: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                found.append(item)

        return found

    def next_link(self) -> str:
        return self.str("_links", "next")

    def title(self) -> str:
        return self.str("title")

    def version_number(self) -> int:
        return self.int("version", "number")

    def last_modified(self) -> str:
        return self.str("version", "when")

    def space_key(self) -> str:
        return self.str("space", "key")

    def webui(self) -> str:
        return self.str("_links", "webui")

    def body_html(self, body_format: str) -> str:
        return self.str("body", body_format, "value")

    def ancestor_titles(self) -> tuple[str, ...]:
        titles: list[str] = []
        for ancestor in self.list("ancestors"):
            title = JsonNode(ancestor).str("title").strip()
            if title:
                titles.append(title)

        return tuple(titles)

    def _value(self, *path: str) -> Any:
        if not path:
            return self._data

        return self.dict(*path[:-1]).get(path[-1])


class ConfluenceJsonDecoder(Decoder):
    """Confluence REST JSON страницы -> HTML-handle + расширенная metadata.

    Вынимает HTML из body.<body_format>.value и считает хэш заголовка с телом
    (TransportKeys.BODY_HASH); обогащает metadata: title (ReaderKeys.PAGE_TITLE),
    version (ConfluenceKeys.VERSION), space, ancestors, source_url,
    last_modified (HttpKeys.LAST_MODIFIED, если ещё не заполнен транспортом).
    """

    DECODER_ID: ClassVar[DecoderId] = DecoderId("ext.confluence_json")

    _HTML_CONTENT_TYPE: ClassVar[str] = "text/html"
    """После JSON->HTML распаковки handle содержит HTML; CONTENT_TYPE приводим к
    этому факту, чтобы DispatchReader мог честно роутить страницы через
    HTMLReader (а не через несуществующий JSONReader)."""

    def __init__(self, *, profile: HttpConnection, body_format: str) -> None:
        self._profile = profile
        self._body_format = body_format
        self._hasher = BodyHasher()

    def decoder_id(self) -> DecoderId:
        return self.DECODER_ID

    async def decode(self, value: RawDocument) -> RawDocument:
        payload = await value.handle.read()
        try:
            content = ConfluenceContent.model_validate_json(payload)
        except ValidationError as e:
            head = payload[:200]
            msg = (
                f"ConfluenceJsonDecoder: decoding page {value.source_id} expected "
                f"a JSON page from Confluence, got {head!r}: {e}"
            )
            raise ConfluencePayloadError(msg) from e

        html = content.body_html(self._body_format).encode(BodyEncoding.UTF8)

        # заголовок сидит в heading_path чанков: переименование меняет хэш
        titled = content.title.encode(BodyEncoding.UTF8) + b"\n" + html
        meta = value.metadata.set(TransportKeys.CONTENT_TYPE, self._HTML_CONTENT_TYPE)
        meta = meta.set(TransportKeys.BODY_HASH, self._hasher.hexdigest(titled))
        if content.title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, content.title)

        meta = meta.set(ConfluenceKeys.VERSION, content.version.number)
        if content.version.when and not meta.has(HttpKeys.LAST_MODIFIED):
            meta = meta.set(HttpKeys.LAST_MODIFIED, content.version.when)

        if content.space.key:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, content.space.key)

        if titles := content.ancestor_titles():
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, titles)

        if labels := content.label_names():
            meta = meta.set(ConfluenceKeys.LABELS, labels)

        if content.links.webui:
            url = str(self._profile.url_of(content.links.webui))
            meta = meta.set(ConfluenceKeys.SOURCE_URL, url)

        return replace(value, handle=ChunkStream.of(html), metadata=meta)


class BodyEncoding(StrEnum):
    """Кодировка тел, которые собирает сам транспорт."""

    UTF8 = "utf-8"


class RunningDigest:
    """Отпечаток тела, которое приходит чанками: update() по мере чтения,
    hexdigest() в конце. Создаётся BodyHasher.stream()."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def update(self, chunk: bytes) -> None:
        self._hash.update(chunk)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class BodyHasher:
    """sha256 тел: один алгоритм на страницы, вложения и хэши индекса.

    Объявляется в конструкторе владельца (ридер, парсер, декодер, транспорт):
    так по конструктору видно, что компонент считает отпечатки. Готовые байты
    и текст хэшируются сразу, поток — через stream().
    """

    def hexdigest(self, payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def text(self, value: str) -> str:
        return self.hexdigest(value.encode(BodyEncoding.UTF8))

    def stream(self) -> RunningDigest:
        return RunningDigest()
