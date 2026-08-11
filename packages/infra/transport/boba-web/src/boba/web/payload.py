"""Операции web внутри песочницы: скачивание, конверсия и поиск по странице.

Окно строк уходит в tool_payload текстом, совпадения — строками NDJSON;
квитанция несёт счётчики. Уход потребителя канала данных прекращает продукцию,
а квитанция всё равно отдаёт то, что успело уехать.

Ошибки: web_request_failed — страница не скачалась (сеть, TLS, HTTP-статус);
unsupported_request — запрос не отвечает ни одной операции. Оба уходят
конвертом в tool_result без трейсбека.
"""

from __future__ import annotations

import re
import sys
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

import httpx
from pydantic import BaseModel

from boba.toolkit.channels import StreamCodec
from boba.toolkit.payload import (
    PayloadChannels,
    PayloadEntry,
    PayloadError,
    PayloadOutputClosedError,
    PayloadStream,
)
from boba.web.protocol import (
    WebFetchRequest,
    WebFetchTrailer,
    WebGrepRequest,
    WebGrepRow,
    WebGrepTrailer,
    WebOp,
    WebProfile,
)


class WebFailure(StrEnum):
    """Ожидаемые отказы операций: kind конверта tool_result."""

    REQUEST_FAILED = "web_request_failed"
    UNSUPPORTED = "unsupported_request"


@dataclass
class _Pending:
    """Совпадение, которому ещё добираются строки контекста после него."""

    line: int
    content: str
    before: tuple[str, ...]
    after: list[str] = field(default_factory=list)

    def row(self) -> WebGrepRow:
        return WebGrepRow(
            line=self.line,
            content=self.content,
            before=self.before,
            after=tuple(self.after),
        )


class WebOps:
    """Операции над веб-страницей; вызываются диспетчером payload'а."""

    ENCODING: ClassVar[str] = "utf-8"
    HEADING_STYLE: ClassVar[str] = "ATX"

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {}
    """Отказы объявляются на месте через PayloadError: сторонних типов нет."""

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        WebOp.FETCH: WebFetchRequest,
        WebOp.GREP: WebGrepRequest,
    }

    @classmethod
    async def dispatch(
        cls, request: BaseModel, channels: PayloadChannels
    ) -> BaseModel:
        if isinstance(request, WebFetchRequest):
            return await cls.web_fetch(request, channels)

        if isinstance(request, WebGrepRequest):
            return await cls.web_grep(request, channels)

        msg = f"unsupported request model: {type(request).__name__}"
        raise PayloadError(WebFailure.UNSUPPORTED, msg)

    @classmethod
    async def load(
        cls, url: str, profile: WebProfile, *, as_markdown: bool
    ) -> str:
        """Скачать страницу профилем из запроса и при нужде — в markdown."""
        try:
            async with httpx.AsyncClient(
                timeout=profile.timeout_sec,
                verify=profile.ssl_verify,
                follow_redirects=True,
                auth=profile.auth.httpx_auth(),
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                body = await response.aread()
        except httpx.HTTPError as e:
            msg = f"web request failed: {type(e).__name__}: {e}"
            raise PayloadError(WebFailure.REQUEST_FAILED, msg) from e

        text = body.decode(cls.ENCODING, errors="replace")

        if not as_markdown:
            return text

        import markdownify  # noqa: PLC0415

        return markdownify.markdownify(text, heading_style=cls.HEADING_STYLE)

    @classmethod
    async def web_fetch(
        cls, request: WebFetchRequest, channels: PayloadChannels
    ) -> WebFetchTrailer:
        """Окно строк уходит текстом; трейлер несёт счётчики для пагинации."""
        page = await cls.load(
            request.url, request.profile, as_markdown=request.as_markdown
        )

        lines = page.splitlines()
        end = request.line_offset + request.line_count
        window = lines[request.line_offset : end]

        written = cls._write_lines(channels.payload(), window)

        return WebFetchTrailer(
            source_url=request.url,
            total_lines=len(lines),
            returned_lines=written,
        )

    @classmethod
    async def web_grep(
        cls, request: WebGrepRequest, channels: PayloadChannels
    ) -> WebGrepTrailer:
        """Каждое совпадение уходит отдельной строкой NDJSON."""
        text = await cls.load(
            request.url, request.profile, as_markdown=request.as_markdown
        )

        pattern = cls.compile_pattern(
            request.pattern,
            fixed_string=request.fixed_string,
            case_insensitive=request.case_insensitive,
        )

        matches = cls.iter_matches(text, pattern, context=request.context)

        cls._write_rows(channels.payload(), matches, request)

        return WebGrepTrailer(source_url=request.url)

    @classmethod
    def _write_lines(cls, stream: PayloadStream, lines: Sequence[str]) -> int:
        """Число уехавших строк; ушедший потребитель обрывает продукцию."""
        written = 0

        for line in lines:
            data = f"{line}\n".encode(cls.ENCODING)

            try:
                stream.write(data)
            except PayloadOutputClosedError:
                return written

            written += 1

        return written

    @classmethod
    def _write_rows(
        cls,
        stream: PayloadStream,
        rows: Iterator[WebGrepRow],
        request: WebGrepRequest,
    ) -> None:
        for emitted, row in enumerate(rows):
            if emitted >= request.limit:
                return

            clipped = cls.clip_row(row, request.max_text_chars)
            line = StreamCodec.encode_row(clipped.model_dump(mode="json"))

            try:
                stream.write(line)
            except PayloadOutputClosedError:
                return

    @staticmethod
    def compile_pattern(
        pattern: str, *, fixed_string: bool, case_insensitive: bool
    ) -> re.Pattern[str]:
        flags = re.NOFLAG
        if case_insensitive:
            flags = re.IGNORECASE

        if fixed_string:
            return re.compile(re.escape(pattern), flags)

        try:
            return re.compile(pattern, flags)
        except re.error:
            return re.compile(re.escape(pattern), flags)

    @classmethod
    def iter_matches(
        cls, text: str, compiled: re.Pattern[str], *, context: int
    ) -> Iterator[WebGrepRow]:
        """Совпадения в порядке появления; контекст добирается следующими строками."""
        window = 0
        if context > 0:
            window = context

        before: deque[str] = deque(maxlen=window)
        pending: list[_Pending] = []

        for number, line in enumerate(text.splitlines(), start=1):
            yield from cls._advance(pending, line, context)

            if compiled.search(line):
                item = _Pending(line=number, content=line, before=tuple(before))

                if context > 0:
                    pending.append(item)
                else:
                    yield item.row()

            before.append(line)

        for item in pending:
            yield item.row()

    @staticmethod
    def _advance(
        pending: list[_Pending], line: str, context: int
    ) -> Iterator[WebGrepRow]:
        """Добирает строку контекста ждущим совпадениям и отдаёт добравшие своё."""
        ready: list[_Pending] = []

        for item in pending:
            if len(item.after) < context:
                item.after.append(line)

            if len(item.after) >= context:
                ready.append(item)

        for item in ready:
            pending.remove(item)
            yield item.row()

    @staticmethod
    def clip_row(row: WebGrepRow, limit: int) -> WebGrepRow:
        """Обрезка длинных строк совпадения потолком конфига."""
        before: list[str] = []
        for line in row.before:
            before.append(line[:limit])

        after: list[str] = []
        for line in row.after:
            after.append(line[:limit])

        return WebGrepRow(
            line=row.line,
            content=row.content[:limit],
            before=tuple(before),
            after=tuple(after),
        )


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(WebOps))
