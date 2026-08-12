"""Операции Confluence: запрос к REST и разбор ответа идут из песочницы.

Приложение отдаёт сюда базовый URL и профиль соединения, а обратно получает
уже готовый текст или строки таблицы. Пагинацию, разбор JSON и HTML делает
payload: наружу не уезжает ни сырой ответ, ни исходная разметка страницы.

Ошибки:
confluence_request_failed — REST недоступен или ответил статусом;
    attachment_not_found — вложения с таким именем на странице нет;
    document_unreadable — вложение скачалось, но не разбирается.
ChannelError — в tool_args приехал запрос чужой модели.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator, Mapping
from typing import Any, ClassVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from boba.text.document import LiteParseError
from boba.tool.kb.confluence.protocol import (
    ConfluenceAttachmentRequest,
    ConfluenceCall,
    ConfluenceContentCall,
    ConfluenceGrepRequest,
    ConfluenceGrepRow,
    ConfluenceNode,
    ConfluencePageCall,
    ConfluencePageRequest,
    ConfluencePageTrailer,
    ConfluenceSearchHit,
    ConfluenceSearchRequest,
    ConfluenceSpace,
    ConfluenceSpacesRequest,
)
from boba.tool.kb.html.payload import PageOps
from boba.toolkit.channels import ByteText, ChannelError, StreamCodec
from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadError
from boba.toolkit.workflow import EmptyTrailer
from boba.web.payload import WebOps


class ConfluenceRest:
    """Пути REST Confluence — те же, что строит приложение."""

    @staticmethod
    def page(page_id: str, body_format: str) -> str:
        expand = (
            f"body.{body_format},version,ancestors,space,metadata.labels,"
            "children.attachment.version,children.attachment.extensions"
        )
        return f"/rest/api/content/{page_id}?expand={expand}"

    @staticmethod
    def search(cql: str, limit: int) -> str:
        expand = "body.view,version,space"
        return (
            f"/rest/api/content/search?cql={quote(cql, safe='')}"
            f"&limit={limit}&expand={expand}"
        )

    @staticmethod
    def spaces(space_type: str, limit: int) -> str:
        type_filter = ""
        if space_type != ConfluenceOps.ANY_SPACE_TYPE:
            type_filter = f"&type={space_type}"
        return f"/rest/api/space?limit={limit}&start=0{type_filter}"


class PageContent:
    """Материализованная страница: её контент и заголовок."""

    def __init__(self, text: str, title: str) -> None:
        self.text = text
        self.title = title


class ConfluenceOps:
    """Операции чтения Confluence; вызываются диспетчером payload'а."""

    ANY_SPACE_TYPE: ClassVar[str] = "any"
    """Значение space_type, при котором REST не фильтруется по типу."""

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        LiteParseError: "document_unreadable",
    }

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {
        ConfluenceNode.PAGE: ConfluencePageRequest,
        ConfluenceNode.GREP: ConfluenceGrepRequest,
        ConfluenceNode.SEARCH: ConfluenceSearchRequest,
        ConfluenceNode.SPACES: ConfluenceSpacesRequest,
        ConfluenceNode.ATTACHMENT: ConfluenceAttachmentRequest,
    }

    @classmethod
    async def dispatch(
        cls,
        request: BaseModel,
        channels: PayloadChannels,
    ) -> BaseModel:
        """Текст уходит байтами, таблицы — строками NDJSON."""
        if isinstance(request, ConfluencePageRequest):
            page = await cls.page(request)
            cls.write_text(channels, page.text)
            return ConfluencePageTrailer(title=page.title)

        if isinstance(request, ConfluenceGrepRequest):
            await cls.grep(request, channels)
            return EmptyTrailer()

        if isinstance(request, ConfluenceSearchRequest):
            await cls.search(request, channels)
            return EmptyTrailer()

        if isinstance(request, ConfluenceSpacesRequest):
            await cls.spaces(request, channels)
            return EmptyTrailer()

        if isinstance(request, ConfluenceAttachmentRequest):
            cls.write_text(channels, await cls.attachment(request))
            return EmptyTrailer()

        msg = f"confluence payload got an unexpected request: {type(request).__name__}"
        raise ChannelError(msg)

    @staticmethod
    def write_text(channels: PayloadChannels, text: str) -> None:
        channels.payload().write(text.encode(ByteText.ENCODING))

    @staticmethod
    def write_rows(channels: PayloadChannels, rows: Iterator[BaseModel]) -> None:
        stream = channels.payload()
        for row in rows:
            stream.write(StreamCodec.encode_row(row.model_dump()))

    @staticmethod
    async def get(request: ConfluenceCall, path: str) -> bytes:
        profile = request.profile
        url = request.base_url.rstrip("/") + path

        try:
            async with httpx.AsyncClient(
                timeout=profile.timeout_sec,
                verify=profile.ssl_verify,
                follow_redirects=True,
                auth=profile.auth.httpx_auth(),
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return await response.aread()
        except httpx.HTTPError as e:
            msg = f"Confluence request failed: {type(e).__name__}: {e}"
            raise PayloadError("confluence_request_failed", msg) from e

    @classmethod
    async def page_json(cls, request: ConfluencePageCall) -> dict[str, Any]:
        path = ConfluenceRest.page(request.page_id, request.body_format)

        return json.loads(await cls.get(request, path))

    @staticmethod
    def body_html(data: Mapping[str, Any], body_format: str) -> str:
        body = data.get("body")
        if not isinstance(body, dict):
            return ""

        view = body.get(body_format)
        if not isinstance(view, dict):
            return ""

        return str(view.get("value") or "")

    @classmethod
    async def page(cls, request: ConfluenceContentCall) -> PageContent:
        data = await cls.page_json(request)
        html = cls.body_html(data, request.body_format)
        title = str(data.get("title") or "")

        if not request.as_markdown:
            return PageContent(text=html, title=title)

        return PageContent(text=PageOps.to_markdown(html), title=title)

    @classmethod
    async def grep(
        cls,
        request: ConfluenceGrepRequest,
        channels: PayloadChannels,
    ) -> None:
        page = await cls.page(request)

        cls.write_rows(channels, cls.matches(request, page.text))

    @classmethod
    def matches(
        cls,
        request: ConfluenceGrepRequest,
        text: str,
    ) -> Iterator[ConfluenceGrepRow]:
        pattern = WebOps.compile_pattern(
            request.pattern,
            fixed_string=request.fixed_string,
            case_insensitive=request.case_insensitive,
        )
        found = WebOps.iter_matches(text, pattern, context=request.context)

        for emitted, row in enumerate(found):
            if emitted >= request.limit:
                return
            clipped = WebOps.clip_row(row, request.max_text_chars)
            yield ConfluenceGrepRow.model_validate(clipped)

    @classmethod
    async def search(
        cls,
        request: ConfluenceSearchRequest,
        channels: PayloadChannels,
    ) -> None:
        path = ConfluenceRest.search(request.cql, request.limit)
        data = json.loads(await cls.get(request, path))

        links = data.get("_links")
        base = request.base_url
        if isinstance(links, dict) and links.get("base"):
            base = str(links["base"])

        cls.write_rows(channels, cls.hits(data, base, request.snippet_chars))

    @classmethod
    def hits(
        cls,
        data: Mapping[str, Any],
        base: str,
        snippet_chars: int,
    ) -> Iterator[ConfluenceSearchHit]:
        for hit in data.get("results") or []:
            yield cls.hit(hit, base, snippet_chars)

    @classmethod
    def hit(
        cls,
        hit: Mapping[str, Any],
        base: str,
        snippet_chars: int,
    ) -> ConfluenceSearchHit:
        html = cls.body_html(hit, "view")

        excerpt = ""
        if html:
            excerpt = PageOps.plain_text(html)

        if len(excerpt) > snippet_chars:
            excerpt = excerpt[: snippet_chars - 1].rstrip() + "…"

        space = hit.get("space")
        space_key = ""
        if isinstance(space, dict):
            space_key = str(space.get("key") or "")

        links = hit.get("_links")
        webui = ""
        if isinstance(links, dict):
            webui = str(links.get("webui") or "")

        url = base
        if webui:
            url = f"{base}{webui}"

        return ConfluenceSearchHit(
            page_id=str(hit.get("id") or ""),
            title=str(hit.get("title") or ""),
            space_key=space_key,
            url=url,
            excerpt=excerpt,
        )

    @classmethod
    async def spaces(
        cls,
        request: ConfluenceSpacesRequest,
        channels: PayloadChannels,
    ) -> None:
        path = ConfluenceRest.spaces(request.space_type, request.limit)
        data = json.loads(await cls.get(request, path))

        cls.write_rows(channels, cls.space_rows(data))

    @staticmethod
    def space_rows(data: Mapping[str, Any]) -> Iterator[ConfluenceSpace]:
        for space in data.get("results") or []:
            yield ConfluenceSpace(
                key=str(space.get("key") or ""),
                name=str(space.get("name") or ""),
                type=str(space.get("type") or ""),
            )

    @classmethod
    async def attachment(cls, request: ConfluenceAttachmentRequest) -> str:
        """Вложение скачивается и парсится здесь же: наружу едет только текст."""
        data = await cls.page_json(request)

        link = cls.attachment_link(data, request.filename)
        if not link:
            msg = (
                f"attachment {request.filename!r} not found "
                f"on page {request.page_id!r}"
            )
            raise PayloadError("attachment_not_found", msg)

        content = await cls.get(request, link)

        from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415

        # парсер нативный и держит GIL: без потока он застопорит loop payload'а
        result = await asyncio.to_thread(
            LiteParseEngine.parse_bytes,
            request.parse_params(),
            content,
            request.filename,
        )

        return result.text

    @staticmethod
    def attachment_link(data: Mapping[str, Any], filename: str) -> str:
        children = data.get("children")
        if not isinstance(children, dict):
            return ""

        attachments = children.get("attachment")
        if not isinstance(attachments, dict):
            return ""

        for item in attachments.get("results") or []:
            if str(item.get("title") or "") != filename:
                continue
            links = item.get("_links")
            if isinstance(links, dict):
                return str(links.get("download") or "")

        return ""


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(ConfluenceOps))
