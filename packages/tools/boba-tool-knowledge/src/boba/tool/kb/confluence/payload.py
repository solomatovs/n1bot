"""Операции Confluence: запрос к REST и разбор ответа идут из песочницы.

Приложение отдаёт сюда базовый URL и профиль соединения, а обратно получает
уже готовый текст или строки таблицы. Пагинацию, разбор JSON и HTML делает
payload: наружу не уезжает ни сырой ответ, ни исходная разметка страницы.
"""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from boba.toolkit.launcher import RowStream
from boba.toolkit.payload import ChunkEmitter, PayloadEntry
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
        type_filter = "" if space_type == "any" else f"&type={space_type}"
        return f"/rest/api/space?limit={limit}&start=0{type_filter}"


class ConfluenceOps:
    """Операции чтения Confluence; вызываются диспетчером payload'а."""

    OPS: ClassVar[tuple[str, ...]] = (
        "confluence_page",
        "confluence_grep",
        "confluence_search",
        "confluence_spaces",
        "confluence_attachment",
    )

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        """Текст уходит кадрами-кусками, таблицы — кадрами-записями."""
        op = request["op"]
        if op == "confluence_page":
            data = cls.page(request)
            PayloadEntry.emit_text(emit, data["text"])
            return {"title": data["title"]}
        if op == "confluence_grep":
            cls.grep(request, emit)
            return {}
        if op == "confluence_search":
            cls.search(request, emit)
            return {}
        if op == "confluence_spaces":
            cls.spaces(request, emit)
            return {}
        if op == "confluence_attachment":
            PayloadEntry.emit_text(emit, cls.attachment(request)["text"])
            return {}
        msg = f"unknown confluence op: {op!r}"
        raise ValueError(msg)

    @staticmethod
    def get(request: dict[str, Any], path: str) -> httpx.Response:
        profile = request["profile"]
        url = request["base_url"].rstrip("/") + path
        try:
            response = httpx.get(
                url,
                timeout=profile["timeout_sec"],
                verify=profile["ssl_verify"],
                follow_redirects=True,
                auth=WebOps.auth_of(profile["auth"]),
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            msg = f"Confluence request failed: {type(e).__name__}: {e}"
            raise RuntimeError(msg) from e
        return response

    @classmethod
    def page_json(cls, request: dict[str, Any]) -> dict[str, Any]:
        path = ConfluenceRest.page(request["page_id"], request["body_format"])
        return json.loads(cls.get(request, path).content)

    @staticmethod
    def body_html(data: dict[str, Any], body_format: str) -> str:
        body = data.get("body")
        if not isinstance(body, dict):
            return ""
        view = body.get(body_format)
        if not isinstance(view, dict):
            return ""
        return str(view.get("value") or "")

    @classmethod
    def page(cls, request: dict[str, Any]) -> dict[str, Any]:
        data = cls.page_json(request)
        html = cls.body_html(data, request["body_format"])
        title = str(data.get("title") or "")
        if not request["as_markdown"]:
            return {"text": html, "title": title}
        from boba.tool.kb.html.payload import PageOps  # noqa: PLC0415

        answer = PageOps.to_markdown({"html": html, "heading_style": "ATX"})
        return {"text": answer["markdown"], "title": title}

    @classmethod
    def grep(cls, request: dict[str, Any], emit: ChunkEmitter) -> None:
        text = cls.page(request)["text"]
        pattern = WebOps.compile_pattern(
            request["pattern"],
            fixed_string=request["fixed_string"],
            case_insensitive=request["case_insensitive"],
        )
        matches = WebOps.iter_matches(text, pattern, context=request["context"])
        for emitted, row in enumerate(matches):
            if emitted >= request["limit"]:
                break
            emit(RowStream.encode(WebOps.clip_row(row, request["max_text_chars"])))

    @classmethod
    def search(cls, request: dict[str, Any], emit: ChunkEmitter) -> None:
        path = ConfluenceRest.search(request["cql"], request["limit"])
        response = cls.get(request, path)
        data = json.loads(response.content)
        base = str(data.get("_links", {}).get("base") or request["base_url"])
        for hit in data.get("results") or []:
            emit(RowStream.encode(cls.hit(hit, base, request["snippet_chars"])))

    @classmethod
    def hit(cls, hit: dict[str, Any], base: str, snippet_chars: int) -> dict[str, Any]:
        from boba.tool.kb.html.payload import PageOps  # noqa: PLC0415

        html = cls.body_html(hit, "view")
        excerpt = ""
        if html:
            excerpt = PageOps.plain_text({"html": html})["text"]
        if len(excerpt) > snippet_chars:
            excerpt = excerpt[: snippet_chars - 1].rstrip() + "…"
        space = hit.get("space")
        space_key = ""
        if isinstance(space, dict):
            space_key = str(space.get("key") or "")
        webui = str(hit.get("_links", {}).get("webui") or "")
        return {
            "page_id": str(hit.get("id") or ""),
            "title": str(hit.get("title") or ""),
            "space_key": space_key,
            "url": f"{base}{webui}" if webui else base,
            "excerpt": excerpt,
        }

    @classmethod
    def spaces(cls, request: dict[str, Any], emit: ChunkEmitter) -> None:
        path = ConfluenceRest.spaces(request["space_type"], request["limit"])
        data = json.loads(cls.get(request, path).content)
        for space in data.get("results") or []:
            row = {
                "key": str(space.get("key") or ""),
                "name": str(space.get("name") or ""),
                "type": str(space.get("type") or ""),
            }
            emit(RowStream.encode(row))

    @classmethod
    def attachment(cls, request: dict[str, Any]) -> dict[str, Any]:
        """Вложение скачивается и парсится здесь же: наружу едет только текст."""
        data = cls.page_json(request)
        filename = request["filename"]
        link = cls.attachment_link(data, filename)
        if not link:
            msg = f"attachment {filename!r} not found on page {request['page_id']!r}"
            raise RuntimeError(msg)
        content = cls.get(request, link).content
        from boba.liteparse.engine import LiteParseEngine  # noqa: PLC0415
        from boba.text.document import LiteParseParams  # noqa: PLC0415

        params = LiteParseParams.model_validate(request["params"])
        result = LiteParseEngine.parse_bytes(params, content, filename)
        return {"text": result.text}

    @staticmethod
    def attachment_link(data: dict[str, Any], filename: str) -> str:
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
    sys.exit(PayloadEntry.main(ConfluenceOps.dispatch))
