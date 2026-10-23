"""Test MCP servers on fastmcp[apps]: neighbours of the MCP client tests that
know nothing about boba. One process per server, as the node servers of this
image:

python apps_server.py prefab <port>    Prefab tools of a plain FastMCP: sales
                                       (DataTable with search, text for the
                                       model), embed (Embed and
                                       csp.frame_domains), clipboard
                                       (permissions and prefersBorder), huge
                                       (custom HTML resource of HUGE_MB);
                                       GET /stats counts the calls per tool
python apps_server.py notes <port>     FastMCPApp "Notes": ui entry notes_board,
                                       backend save and list_notes
python apps_server.py genui <port>     the GenerativeUI provider
python apps_server.py renderer <port>  static dist of @prefecthq/prefab-ui with
                                       CORS: the renderer of the three servers

The renderer address comes from PREFAB_RENDERER_URL (prefab_ui.renderer reads
it): the servers answer neither with the CDN stub nor with the bundled page.
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.apps.app import FastMCPApp
from fastmcp.apps.config import (
    UI_MIME_TYPE,
    AppConfig,
    PrefabAppConfig,
    ResourceCSP,
    ResourcePermissions,
)
from fastmcp.apps.generative import GenerativeUI
from fastmcp.tools.base import ToolResult
from prefab_ui.actions.mcp import CallTool
from prefab_ui.actions.state import SetState
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Button,
    Column,
    DataTable,
    DataTableColumn,
    Embed,
    ExpandableRow,
    Form,
    Heading,
    Input,
    Text,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

MCP_PATH = "/mcp"
STATS_PATH = "/stats"
HUGE_URI = "ui://stand/huge.html"
HUGE_MB = 12
EMBEDDED = "https://maps.example.com"
RENDERER_DIR = Path("/srv/prefab-ui")

SALES: list[dict[str, Any]] = [
    {"region": "North", "customer": "Acme", "total": 120.5},
    {"region": "North", "customer": "Umbrella", "total": 15.0},
    {"region": "South", "customer": "Globex", "total": 89.9},
    {"region": "South", "customer": "Initech", "total": 42.0},
    {"region": "West", "customer": "Hooli", "total": 30.0},
]


class CallCounts:
    """tools/call per tool name of one server, for GET /stats."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def hit(self, tool: str) -> None:
        self._counts[tool] = self._counts.get(tool, 0) + 1

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


class PrefabServer:
    """Plain FastMCP whose tools draw Prefab views and custom HTML."""

    def __init__(self) -> None:
        self.mcp = FastMCP("prefab")
        self._counts = CallCounts()
        self.mcp.tool(app=True)(self.sales)
        self.mcp.tool(app=PrefabAppConfig(csp=ResourceCSP(frame_domains=[EMBEDDED])))(
            self.embed
        )
        self.mcp.tool(
            app=PrefabAppConfig(
                permissions=ResourcePermissions(clipboard_write={}),
                prefers_border=False,
            )
        )(self.clipboard)
        self.mcp.tool(app=AppConfig(resource_uri=HUGE_URI))(self.huge)
        self.mcp.resource(
            HUGE_URI,
            mime_type=UI_MIME_TYPE,
            meta={"ui": {"csp": {"resourceDomains": []}}},
        )(self.huge_page)
        self.mcp.custom_route(STATS_PATH, methods=["GET"])(self.stats)

    def sales(self, region: str = "All") -> ToolResult:
        """Sales by customer as a searchable table; region narrows the rows."""
        self._counts.hit("sales")
        rows = self._rows(region)
        with PrefabApp() as table, Column(gap=4):
            Heading("Sales")
            DataTable(
                columns=[
                    DataTableColumn(key="region", header="Region", sortable=True),
                    DataTableColumn(key="customer", header="Customer"),
                    DataTableColumn(key="total", header="Total"),
                ],
                rows=rows,
                search=True,
            )

        return ToolResult(
            content=f"{len(rows)} sales rows are shown by their table",
            structured_content=table,
        )

    def embed(self, place: str) -> PrefabApp:
        """A map of the place embedded from another origin."""
        self._counts.hit("embed")
        with PrefabApp() as page, Column(gap=4):
            Text(f"Map of {place}")
            Embed(url=f"{EMBEDDED}/view?q={place}", height="200px")

        return page

    def clipboard(self) -> PrefabApp:
        """A view that asks the host for clipboard-write and no border."""
        self._counts.hit("clipboard")

        return PrefabApp(view=Text("copy me"))

    def huge(self) -> str:
        """Opens the oversized page."""
        self._counts.hit("huge")

        return "the huge page is shown by its app"

    def huge_page(self) -> str:
        filler = "<!-- " + "x" * 1024 + " -->\n"
        body = filler * (HUGE_MB * 1024)

        return f"<!DOCTYPE html><html><body><p id='huge'>huge</p>{body}</body></html>"

    async def stats(self, request: Request) -> Response:
        return JSONResponse(self._counts.snapshot())

    @staticmethod
    def _rows(region: str) -> list[dict[str, Any] | ExpandableRow]:
        if region == "All":
            return list(SALES)

        return [row for row in SALES if row["region"] == region]


class NotesServer:
    """FastMCPApp: the model opens the board, the board saves through the
    backend and redraws its list from the backend's answer."""

    def __init__(self) -> None:
        self.mcp = FastMCP("notes")
        self._notes: list[str] = ["buy milk"]
        self.app = FastMCPApp("Notes")
        self.app.ui()(self.notes_board)
        self.app.tool()(self.save)
        self.app.tool()(self.list_notes)
        self.mcp.add_provider(self.app)

    def notes_board(self) -> PrefabApp:
        """The notes board with a form that saves a new note."""
        saved = CallTool(
            "save",
            arguments={"text": "{{ text }}"},
            on_success=[SetState("notes", "{{ $result.notes }}"), SetState("text", "")],
        )
        state = {"notes": self._rows(), "text": ""}
        with PrefabApp(state=state) as board, Column(gap=4):
            Heading("Notes")
            with Form():
                Input(name="text", placeholder="New note")
                Button("Save", on_click=saved)
            DataTable(
                columns=[DataTableColumn(key="text", header="Note")],
                rows="{{ notes }}",
            )

        return board

    def save(self, text: str) -> dict[str, Any]:
        """Saves a note and answers with all notes."""
        self._notes.append(text)

        return {"notes": self._rows()}

    def list_notes(self) -> dict[str, Any]:
        """All notes."""
        return {"notes": self._rows()}

    def _rows(self) -> list[dict[str, str]]:
        return [{"text": note} for note in self._notes]


class GenerativeServer:
    """The GenerativeUI provider as shipped."""

    def __init__(self) -> None:
        self.mcp = FastMCP("genui")
        self.mcp.add_provider(GenerativeUI())


class RendererPages(SimpleHTTPRequestHandler):
    """Static dist of the renderer with CORS, as nginx of an apps host would
    serve it: module scripts with crossorigin need the header."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "public, max-age=86400")
        super().end_headers()


def serve_renderer(port: int) -> None:
    handler = partial(RendererPages, directory=str(RENDERER_DIR))
    # the container's address is the one the browser loads the renderer from
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()  # noqa: S104


def main(argv: list[str]) -> None:
    kind, port = argv[1], int(argv[2])
    if kind == "renderer":
        serve_renderer(port)
        return

    servers = {
        "prefab": PrefabServer,
        "notes": NotesServer,
        "genui": GenerativeServer,
    }
    server = servers[kind]()
    server.mcp.run(
        transport="http",
        show_banner=False,
        host="0.0.0.0",  # noqa: S104 — the servers answer on the container's address
        port=port,
        path=MCP_PATH,
    )


if __name__ == "__main__":
    main(sys.argv)
