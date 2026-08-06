"""Операции web: скачивание, конверсия и поиск идут из песочницы.

Ошибки: web_request_failed — страница не скачалась (сеть, TLS, HTTP-статус);
это состояние внешнего мира, поэтому едет пользователю текстом без трейсбека.
"""

from __future__ import annotations

import re
import sys
from collections import deque
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

import httpx

from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry, PayloadError


class BearerAuth(httpx.Auth):
    """Authorization: Bearer <token>; в httpx такого класса нет."""

    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


class WebOps:
    """Операции над веб-страницей; вызываются диспетчером payload'а."""

    OPS: ClassVar[tuple[str, ...]] = ("web_fetch", "web_grep")
    ENCODING: ClassVar[str] = "utf-8"
    HEADING_STYLE: ClassVar[str] = "ATX"

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {}
    """Отказы объявляются на месте через PayloadError: сторонних типов нет."""

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        op = request["op"]
        if op == "web_fetch":
            return await cls.web_fetch(request, emit)
        if op == "web_grep":
            return await cls.web_grep(request, emit)
        msg = f"unknown web op: {op!r}"
        raise ValueError(msg)

    @classmethod
    async def load(cls, request: dict[str, Any]) -> str:
        """Скачать страницу профилем из запроса и при нужде — в markdown."""
        profile = request["profile"]
        try:
            async with httpx.AsyncClient(
                timeout=profile["timeout_sec"],
                verify=profile["ssl_verify"],
                follow_redirects=True,
                auth=cls.auth_of(profile["auth"]),
            ) as client:
                response = await client.get(request["url"])
                response.raise_for_status()
                body = await response.aread()
        except httpx.HTTPError as e:
            msg = f"web request failed: {type(e).__name__}: {e}"
            raise PayloadError("web_request_failed", msg) from e
        text = body.decode(cls.ENCODING, errors="replace")
        if not request["as_markdown"]:
            return text
        import markdownify  # noqa: PLC0415

        return markdownify.markdownify(text, heading_style=cls.HEADING_STYLE)

    @staticmethod
    def auth_of(auth: dict[str, str]) -> Any:
        """Метод авторизации профиля -> клиент httpx."""
        method = auth["method"]
        if method == "none":
            return None
        if method == "basic":
            return httpx.BasicAuth(auth["user"], auth["password"])
        if method == "digest":
            return httpx.DigestAuth(auth["user"], auth["password"])
        if method == "bearer":
            return BearerAuth(auth["token"])
        msg = f"unknown auth method: {method!r}"
        raise ValueError(msg)

    @classmethod
    async def web_fetch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        """Окно строк уходит кадрами построчно; трейлер несёт счётчики."""
        page = await cls.load(request)
        lines = page.splitlines()
        offset = request["line_offset"]
        window = lines[offset : offset + request["line_count"]]
        last = len(window) - 1
        for index, line in enumerate(window):
            if index < last:
                emit(f"{line}\n")
                continue
            if line:
                emit(line)
        return {
            "source_url": request["url"],
            "total_lines": len(lines),
            "returned_lines": len(window),
        }

    @classmethod
    async def web_grep(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        """Каждое совпадение уходит отдельным кадром-записью."""
        text = await cls.load(request)
        pattern = cls.compile_pattern(
            request["pattern"],
            fixed_string=request["fixed_string"],
            case_insensitive=request["case_insensitive"],
        )
        matches = cls.iter_matches(text, pattern, context=request["context"])
        for emitted, row in enumerate(matches):
            if emitted >= request["limit"]:
                break
            emit(RowStream.encode(cls.clip_row(row, request["max_text_chars"])))
        return {"source_url": request["url"]}

    @staticmethod
    def compile_pattern(
        pattern: str, *, fixed_string: bool, case_insensitive: bool
    ) -> re.Pattern[str]:
        flags = re.IGNORECASE if case_insensitive else 0
        if fixed_string:
            return re.compile(re.escape(pattern), flags)
        try:
            return re.compile(pattern, flags)
        except re.error:
            return re.compile(re.escape(pattern), flags)

    @staticmethod
    def iter_matches(
        text: str, compiled: re.Pattern[str], *, context: int
    ) -> Iterator[dict[str, Any]]:
        before: deque[str] = deque(maxlen=context if context > 0 else 0)
        after_needed: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            for row in after_needed:
                if len(row["after"]) < context:
                    row["after"].append(line)
            ready: list[dict[str, Any]] = []
            for row in after_needed:
                if len(row["after"]) >= context:
                    ready.append(row)
            for row in ready:
                after_needed.remove(row)
                yield row
            if compiled.search(line):
                row = {
                    "line": number,
                    "content": line,
                    "before": list(before),
                    "after": [],
                }
                if context > 0:
                    after_needed.append(row)
                else:
                    yield row
            before.append(line)
        for row in after_needed:
            yield row

    @staticmethod
    def clip_row(row: dict[str, Any], limit: int) -> dict[str, Any]:
        before: list[str] = []
        for line in row["before"]:
            before.append(line[:limit])
        after: list[str] = []
        for line in row["after"]:
            after.append(line[:limit])
        return {
            "line": row["line"],
            "content": row["content"][:limit],
            "before": before,
            "after": after,
        }


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(WebOps))
