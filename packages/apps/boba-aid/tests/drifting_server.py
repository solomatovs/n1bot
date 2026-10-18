"""Обычный MCP-сервер на официальном SDK без потоков смены списков.

Запускается субпроцессом по stdio: `python drifting_server.py <каталог>`.
Каталог состояния ведёт тест: instructions.txt — инструкции сервера, их он
читает на старте; файл down — сервер недоступен и выходит сразу на старте.
Потока subscriptions/listen у сервера нет, поэтому смену списков он не
обещает (listChanged false): drift добавляет или убирает инструмент extra,
publish добавляет ресурс notes, instruct меняет инструкции server/discover —
молча, без уведомлений; die обрывает процесс посреди вызова.
"""

import os
import sys
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources.types import TextResource


class DriftingFile(StrEnum):
    """Файлы каталога состояния сервера."""

    INSTRUCTIONS = "instructions.txt"
    DOWN = "down"


class DriftingServer:
    """Сервер с инструментами echo, drift, publish, instruct и die."""

    EXTRA: ClassVar[str] = "extra"
    NOTES: ClassVar[str] = "drifting://notes"
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, state: Path) -> None:
        instructions = (state / DriftingFile.INSTRUCTIONS).read_text(self.ENCODING)
        self._mcp = MCPServer(
            "drifting", instructions=instructions, version="1.0", subscriptions=False
        )
        self._extra_on = False
        self._mcp.tool()(self.echo)
        self._mcp.tool()(self.drift)
        self._mcp.tool()(self.publish)
        self._mcp.tool()(self.instruct)
        self._mcp.tool()(self.die)

    def echo(self, text: str) -> str:
        """Returns the text as is."""
        return text

    def drift(self) -> str:
        """Adds or removes the tool `extra` without notifying the client."""
        if self._extra_on:
            self._mcp.remove_tool(self.EXTRA)
            self._extra_on = False
        else:
            self._mcp.add_tool(self.extra, name=self.EXTRA)
            self._extra_on = True

        return f"extra on: {self._extra_on}"

    def extra(self) -> str:
        """Appears only after drift."""
        return "extra answered"

    def publish(self) -> str:
        """Adds the resource drifting://notes without notifying the client."""
        notes = TextResource(
            uri=self.NOTES, name="notes", text="the notes of the drifting server"
        )
        self._mcp.add_resource(notes)
        return self.NOTES

    def instruct(self, text: str) -> str:
        """Replaces the instructions of the server without notifying the client."""
        self._mcp._lowlevel_server.instructions = text
        return f"instructions: {text}"

    def die(self) -> str:
        """Kills the server in the middle of the call."""
        os._exit(1)

    def run(self) -> None:
        self._mcp.run("stdio")


if __name__ == "__main__":
    state = Path(sys.argv[1])
    if (state / DriftingFile.DOWN).exists():
        sys.exit(f"drifting server: {state / DriftingFile.DOWN} is present, it is down")

    DriftingServer(state).run()
