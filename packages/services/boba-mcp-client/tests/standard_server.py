"""Обычный MCP-сервер на официальном SDK: про boba он ничего не знает.

Запускается субпроцессом из тестов клиента: `python standard_server.py stdio`
либо `python standard_server.py streamable-http <port>`.
"""

import sys

from mcp.server.fastmcp import FastMCP, Image


class StandardServer:
    """Сервер с четырьмя инструментами: число, текст, сбой и картинка."""

    PNG: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    def __init__(self, port: int) -> None:
        self._mcp = FastMCP("standard", host="127.0.0.1", port=port)
        self._mcp.tool()(self.add)
        self._mcp.tool()(self.shout)
        self._mcp.tool()(self.broken)
        self._mcp.tool()(self.picture)

    def add(self, a: int, b: int) -> int:
        """Adds two numbers."""
        return a + b

    def shout(self, text: str) -> str:
        """Returns the text in upper case."""
        return text.upper()

    def broken(self) -> str:
        """Always fails."""
        msg = "no such table: orders"
        raise ValueError(msg)

    def picture(self) -> Image:
        """Returns a tiny image."""
        return Image(data=self.PNG, format="png")

    def run(self, transport: str) -> None:
        if transport == "stdio":
            self._mcp.run("stdio")
            return

        self._mcp.run("streamable-http")


if __name__ == "__main__":
    port = 0
    if len(sys.argv) > 2:
        port = int(sys.argv[2])

    StandardServer(port).run(sys.argv[1])
