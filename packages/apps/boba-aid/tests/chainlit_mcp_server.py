"""Сервер SDK в отдельном процессе: shutdown SSE не переживает свой сервер."""

import argparse
import asyncio
import json
from pathlib import Path

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

server = MCPServer("chainlit-patch-check")


@server.tool()
def echo(text: str) -> str:
    return text


class RecordedRequests:
    def __init__(self, app: ASGIApp, log: Path) -> None:
        self._app = app
        self._log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        authorization = headers.get(b"authorization", b"").decode()
        row = {"method": scope["method"], "authorization": authorization}
        with self._log.open("a") as output:
            output.write(json.dumps(row) + "\n")

        if authorization != "Bearer patch-fixture":
            denied = PlainTextResponse("Missing fixture authorization", status_code=401)
            await denied(scope, receive, send)
            return

        await self._app(scope, receive, send)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transport", nargs="?", default="stdio")
    parser.add_argument("port", nargs="?", type=int, default=0)
    parser.add_argument("--request-log", type=Path)
    args = parser.parse_args()
    if args.transport == "stdio":
        asyncio.run(server.run_stdio_async())
    else:
        if args.transport == "sse":
            app = server.sse_app()
        else:
            app = server.streamable_http_app()

        recorded = RecordedRequests(app, args.request_log)
        uvicorn.run(recorded, host="127.0.0.1", port=args.port, log_level="warning")
