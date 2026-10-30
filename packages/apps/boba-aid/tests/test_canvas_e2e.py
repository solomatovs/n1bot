"""Канвас в живом браузере: что реально попало в DOM панели по типам файлов.

Поднимает приложение, логинится локальным пользователем, кладёт файлы в
workspace открытого треда и просит панель показать каждый — затем проверяет
DOM: картинка загрузилась, диаграмма отрисована в svg, текст виден,
неподдерживаемый формат объяснён.

Запуск: BOBA_CONFIG_PATH=... pytest -m integration
packages/apps/boba-aid/tests/test_canvas_e2e.py
Стенд — два собственных процесса на конфигах отладки (файлы приложений из
BOBA_CONFIG_PATH и BOBA_MCP_CONFIG_PATH): сервис boba-mcp и чат, который ходит
к этому сервису. Порты, каталоги данных и база у стенда свои (E2eStand):
прогон не занимает адреса отладки из конфигов и не трогает её данные;
контейнеры compose не участвуют.
Нужны: playwright + chromium, postgres, образ workspace из runtime/sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx
import pytest
from aid_stand import AppLanguage, FakeUrl
from playwright.async_api import Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from boba.aid.canvas.panel import CanvasNote
from boba.config import ConfigError
from boba.stand.names import StandNames
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.stand import (
    StandApp,
    StandError,
    StandFiles,
    StandOverlay,
    StandPorts,
)
from boba.toolkit.result import ContentMime

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPO = Path(__file__).resolve().parents[4]
LAUNCHER = REPO / ".venv/bin/python"
ENTRY = REPO / "packages/apps/boba-aid/src/boba/aid/main.py"
MCP_MODULE = "boba.mcp_server"


class E2eTexts:
    """Подписи панели на языке приложения: текст до первой подстановки."""

    DIAGRAM_FAILED: ClassVar[str] = "aid.canvas.diagramFailed"
    PLACEHOLDER: ClassVar[str] = "{{"

    def head(self, key: str) -> str:
        return AppLanguage().texts().text(key).split(self.PLACEHOLDER)[0]


class E2eStand:
    """Адреса, каталоги и база стенда e2e поверх конфигов отладки.

    Конфиги отладки называют порты, данные и базу интерактивной отладки; стенд
    кладёт поверх них наложение в <root>/<приложение>/conf со своей базой,
    портами, префиксами, каталогами данных <root>/<приложение>/data и адресом
    сервиса (отладка ходит в сервис через nginx), чтобы прогон шёл рядом с
    запущенной отладкой. Каталог root — временный каталог прогона, его даёт
    фикстура app_server. Чат и сервис делят одну базу стенда.
    """

    SERVICE_PORT: ClassVar[int] = StandPorts().free()
    SERVICE_HOST: ClassVar[str] = "localhost"
    SERVICE_SCHEME: ClassVar[str] = "http"
    SERVICE_PREFIX: ClassVar[str] = "/mcp"
    APP_PREFIX: ClassVar[str] = "/aid"
    INSTANCE: ClassVar[str] = "e2e"
    SERVICE_LAUNCHER: ClassVar[str] = "sandbox"
    DATA_DIRS: ClassVar[tuple[str, ...]] = ("krb", "dump", "workspace", "tool-logs")
    DB_NAME: ClassVar[str] = "e2e"
    CONF: ClassVar[str] = "conf"
    DATA: ClassVar[str] = "data"

    def __init__(self, root: Path) -> None:
        self._root = root
        self._app_data = root / StandApp.AID.value / self.DATA
        self._service_data = root / StandApp.MCP.value / self.DATA

    def prepare(self) -> None:
        for data in (self._app_data, self._service_data):
            for name in self.DATA_DIRS:
                (data / name).mkdir(parents=True, exist_ok=True)

        database = StandDatabase(StandApp.AID, StandNames().tree_database(self.DB_NAME))
        name = database.prepare()

        self._files(StandApp.MCP).write(self._service_overlay(name))
        self._files(StandApp.AID).write(self._app_overlay(name))

    def arguments(self, app: StandApp) -> list[str]:
        """Аргументы --config приложения стенда: конфиг отладки и наложение,
        записанное prepare."""
        return self._files(app).arguments(app)

    def paths(self, app: StandApp) -> tuple[Path, ...]:
        return self._files(app).paths(app)

    def _files(self, app: StandApp) -> StandFiles:
        return StandFiles(self._root / app.value / self.CONF)

    def _service_overlay(self, database: str) -> StandOverlay:
        origin = f"{self.SERVICE_SCHEME}://{self.SERVICE_HOST}:{self.SERVICE_PORT}"
        overlay = StandOverlay()
        overlay.set(("postgres", "dbname"), database)
        overlay.set(("env", "port"), self.SERVICE_PORT)
        overlay.set(("env", "url_prefix"), self.SERVICE_PREFIX)
        overlay.set(("env", "public_url"), f"{origin}{self.SERVICE_PREFIX}")
        overlay.set(("instance", "data_dir"), str(self._service_data))
        # тесты называют файлы путями тела инструмента (/workspace/...):
        # так их видит только сервис с песочницей
        overlay.set(("env", "tool_launcher"), self.SERVICE_LAUNCHER)

        return overlay

    def _app_overlay(self, database: str) -> StandOverlay:
        overlay = StandOverlay()
        overlay.set(("postgres", "dbname"), database)
        overlay.set(("postgres_history", "dbname"), database)
        overlay.set(("env", "port"), PORT)
        overlay.set(("env", "url_prefix"), self.APP_PREFIX)
        overlay.set(("env", "instance_id"), self.INSTANCE)
        overlay.set(("instance", "data_dir"), str(self._app_data))
        overlay.set(("env", "mcp_scheme"), self.SERVICE_SCHEME)
        overlay.set(("env", "mcp_host"), self.SERVICE_HOST)
        overlay.set(("env", "mcp_port"), self.SERVICE_PORT)
        overlay.set(("env", "mcp_prefix"), self.SERVICE_PREFIX)

        return overlay


PORT = int(os.environ.get("BOBA_E2E_PORT") or StandPorts().free())
"""Порт чата стенда: свободный на момент прогона, если его не назвали явно —
порты конфигов отладки заняты интерактивной отладкой, своей и соседей."""
BASE = FakeUrl.loopback(PORT, E2eStand.APP_PREFIX)
FILES_PROFILE = "general"
"""Профиль, в котором работает стенд: файлы лежат на его первом MCP-сервере."""
LOGIN = ("admin", "myPassdfd3")

FLOW_MMD = "flowchart LR\n    A[Доход] --> B[Налог]\n".encode()
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000a0000000a0806000000"
    "8d32cfbd0000001a49444154789c63fcffff3f0324a6018a03a0d80751"
    "8c00e30600002e2c0201f3ba9d5c0000000049454e44ae426082"
)
SVG = (Path(__file__).parent / "fixtures" / "probe.svg").read_bytes()
BROKEN_MMD = (
    "flowchart LR\n"
    '    A["Доход"] --> B["Вычеты"]\n'
    "\n"
    '    subgraph VYCHE[ "Структура вычетов" ]\n'
    '        B1["Стандартный"]\n'
    "    end\n"
    "    B1 --> B\n"
)
"""Пробелы внутри скобок подграфа — mermaid такое не принимает (реальный случай)."""

HOSTILE_MMD = (
    "flowchart LR\n"
    '    A["<img src=x onerror=window.__boba_pwned=1>Start"] --> B[End]\n'
    "    click A call window.__boba_click()\n"
    '    click B "javascript:window.__boba_pwned=2"\n'
)
"""Спека чужого сервера: HTML в подписи и click — strict обязан их обезвредить."""
HOSTILE_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40" '
    b'onload="window.__boba_pwned=3"><script>window.__boba_pwned=4</script>'
    b'<rect width="120" height="40" fill="#4c7cf0"/></svg>'
)
HOSTILE_HTML = b"<html><body><script>window.__boba_pwned=5</script></body></html>"
PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 45>>stream\n"
    b"BT /F1 20 Tf 20 200 Td (Alpha page one) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"6 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 7 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"7 0 obj<</Length 44>>stream\n"
    b"BT /F1 20 Tf 20 200 Td (Beta page two) Tj ET\n"
    b"endstream endobj\n"
    b"trailer<</Root 1 0 R/Size 8>>\n%%EOF\n"
)
"""Две страницы с текстом: pdf.js восстанавливает таблицу xref сам."""
PDF_REVISED = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\n"
    b"BT /F1 20 Tf 20 200 Td (Gamma revised) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R/Size 6>>\n%%EOF\n"
)
HOSTILE_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R/OpenAction 10 0 R"
    b"/AcroForm<</Fields[9 0 R]/CO[9 0 R]>>>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>/Annots[6 0 R 7 0 R 8 0 R 9 0 R]>>endobj\n"
    b"4 0 obj<</Length 43>>stream\n"
    b"BT /F1 16 Tf 20 260 Td (Hostile page) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"6 0 obj<</Type/Annot/Subtype/Link/Rect[20 200 280 230]/Border[0 0 0]"
    b"/A<</S/JavaScript/JS(window.__boba_pwned=7)>>>>endobj\n"
    b"7 0 obj<</Type/Annot/Subtype/Link/Rect[20 160 280 190]/Border[0 0 0]"
    b"/A<</S/URI/URI(javascript:window.__boba_pwned=6)>>>>endobj\n"
    b"8 0 obj<</Type/Annot/Subtype/Link/Rect[20 120 280 150]/Border[0 0 0]"
    b"/A<</S/URI/URI(https://example.invalid/boba)>>>>endobj\n"
    b"9 0 obj<</Type/Annot/Subtype/Widget/FT/Tx/T(total)/V(1)/F 4/P 3 0 R"
    b"/Rect[20 60 280 90]/AA<</C<</S/JavaScript/JS(window.__boba_pwned=9)>>"
    b"/K<</S/JavaScript/JS(window.__boba_pwned=10)>>>>>>endobj\n"
    b"10 0 obj<</S/JavaScript/JS(app.alert(1);window.__boba_pwned=8)>>endobj\n"
    b"trailer<</Root 1 0 R/Size 11>>\n%%EOF\n"
)
"""Документ чужого сервера: JavaScript при открытии, у ссылки и у поля с
вычислением, ссылка javascript: и внешняя http-ссылка."""
WAV = bytes.fromhex(
    "524946462400000057415645666d74201000000001000100401f0000401f0000"
    "010008006461746100000000"
)

FILES: dict[str, bytes] = {
    "chart.png": PNG,
    "after-notice.png": PNG,
    "hostile.mmd": HOSTILE_MMD.encode(),
    "hostile.svg": HOSTILE_SVG,
    "page.html": HOSTILE_HTML,
    "report.pdf": PDF,
    "report": PDF,
    "hostile.pdf": HOSTILE_PDF,
    "clip.wav": WAV,
    "chart.svg": SVG,
    "flow.mmd": FLOW_MMD,
    "flow-copy.mmd": FLOW_MMD,
    "report.md": "# Отчёт\n\n- доход: 4 786 066\n".encode(),
    "data.bin": b"\x00\x01\x02\x03unsupported",
    "broken.mmd": BROKEN_MMD.encode(),
    "notes.log": ("строка журнала\n" * 40).encode(),
    "layout.png": PNG,
    "reconnect.png": PNG,
    "outage.png": PNG,
}


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class LogTail:
    """Строки журнала процесса стенда, дописанные после метки: тест ждёт в
    них состояния, которое видно только серверу. Файл читается порциями с
    запомненного смещения и целиком в память не поднимается. Создаёт
    E2eApp.tail."""

    READ_BYTES: ClassVar[int] = 1 << 16
    POLL_SEC: ClassVar[float] = 0.2
    TIMEOUT_SEC: ClassVar[float] = 60.0

    def __init__(self, log: Path, offset: int) -> None:
        self._log = log
        self._offset = offset
        self._rest = b""

    async def until(self, pattern: re.Pattern[str], what: str) -> None:
        """Ждёт строку журнала, в которой есть pattern."""
        deadline = time.monotonic() + self.TIMEOUT_SEC

        while time.monotonic() < deadline:
            for line in self._lines():
                if pattern.search(line):
                    return

            await asyncio.sleep(self.POLL_SEC)

        msg = (
            f"{self._log.name}: expected {what} (a line matching "
            f"{pattern.pattern!r}) within {self.TIMEOUT_SEC:.0f}s, got none"
        )
        raise AssertionError(msg)

    def _lines(self) -> Iterator[str]:
        with self._log.open("rb") as log:
            log.seek(self._offset)
            chunk = log.read(self.READ_BYTES)

        self._offset += len(chunk)
        pieces = (self._rest + chunk).split(b"\n")
        self._rest = pieces.pop()
        for piece in pieces:
            yield piece.decode("utf-8", errors="replace")


class E2eApp:
    """Процесс стенда e2e — чат или сервис boba-mcp: старт своим лончером на
    конфиге отладки, ожидание ответа адреса готовности и остановка с добиванием.

    Под нагрузкой параллельного прогона прогрев зигот растягивает старт, поэтому
    ожидание ограничено сроком, а не числом попыток, и обрывается сразу, если
    процесс умер; остановка, не уложившаяся в срок, добивает процесс. Процесс
    можно остановить и поднять снова — журнал дописывается, а не начинается
    заново."""

    BOOT_SEC: ClassVar[float] = 300.0
    STOP_SEC: ClassVar[float] = 20.0
    POLL_SEC: ClassVar[float] = 0.5
    TAIL_LINES: ClassVar[int] = 40

    def __init__(
        self,
        command: Sequence[str],
        ready_url: str,
        log: Path,
    ) -> None:
        self._command = command
        self._ready_url = ready_url
        self._log = log
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            # тест запускает собственный лончер конфигом стенда
            # nosemgrep: dangerous-subprocess-use-tainted-env-args
            [str(LAUNCHER), *self._command],
            env=dict(os.environ),
            stdout=self._log.open("ab"),
            stderr=subprocess.STDOUT,
        )
        self._await_ready(self._process)

    def stop(self) -> None:
        if self._process is None:
            return

        self._process.terminate()
        try:
            self._process.wait(timeout=self.STOP_SEC)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def tail(self) -> LogTail:
        """Журнал процесса с этого момента: прежние строки не в счёт."""
        offset = 0
        if self._log.is_file():
            offset = self._log.stat().st_size

        return LogTail(self._log, offset)

    def _await_ready(self, process: subprocess.Popen[bytes]) -> None:
        url = self._ready_url
        deadline = time.monotonic() + self.BOOT_SEC
        last_error = "no reply yet"

        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    f"app exited with code {process.returncode} before answering "
                    f"GET {url}:\n{self._tail()}"
                )

            try:
                answer = httpx.get(url, follow_redirects=True, timeout=5.0)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(self.POLL_SEC)
                continue

            if answer.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                return

            last_error = f"HTTP {answer.status_code}"
            time.sleep(self.POLL_SEC)

        pytest.fail(
            f"GET {url}: no reply below 500 within {self.BOOT_SEC:.0f}s, last: "
            f"{last_error}\n{self._tail()}"
        )

    def _tail(self) -> str:
        if not self._log.is_file():
            return ""

        lines = self._log.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-self.TAIL_LINES :])


@dataclass(frozen=True)
class E2eProcesses:
    """Процессы стенда: чат и сервис boba-mcp, к которому он ходит. Отдаёт
    фикстура app_server; тесты читают журнал чата и останавливают сервис."""

    app: E2eApp
    service: E2eApp
    stand: E2eStand


@pytest.fixture(scope="module")
def app_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[E2eProcesses]:
    stand = E2eStand(tmp_path_factory.mktemp("boba-e2e-stand"))
    try:
        stand.prepare()
    except (ConfigError, StandError) as exc:
        pytest.fail(f"файлы конфигурации стенда недоступны: {exc}")

    # чужой процесс на порту молча увёл бы тесты на другой код
    for port in (PORT, stand.SERVICE_PORT):
        probe = socket.socket()
        try:
            taken = probe.connect_ex(("127.0.0.1", port)) == 0
        finally:
            probe.close()

        if taken:
            pytest.fail(f"порт {port} уже занят: остановите запущенное приложение")

    # логи — в каталоге прогона pytest: общий путь в /tmp делят пользователи
    logs = tmp_path_factory.mktemp("boba-e2e")
    service = E2eApp(
        ["-m", MCP_MODULE, *stand.arguments(StandApp.MCP)],
        FakeUrl.loopback(stand.SERVICE_PORT, f"{stand.SERVICE_PREFIX}/health"),
        logs / f"mcp-{stand.SERVICE_PORT}.log",
    )
    app = E2eApp(
        [str(ENTRY), *stand.arguments(StandApp.AID)],
        BASE + "/login",
        logs / f"app-{PORT}.log",
    )
    try:
        service.start()
        app.start()
        yield E2eProcesses(app=app, service=service, stand=stand)
    finally:
        app.stop()
        service.stop()


async def _cookie_header(context: Any) -> str:
    """Все cookie контекста браузера одной строкой заголовка Cookie."""
    pairs: list[str] = []
    for cookie in await context.cookies():
        pairs.append(f"{cookie['name']}={cookie['value']}")

    return "; ".join(pairs)


def _app_config(stand: E2eStand) -> Any:
    from boba.aid.infra.config import AppConfig
    from boba.config import ConfigLoader, bind

    raw = ConfigLoader(stand.paths(StandApp.AID)).load()
    return bind(raw, path="app", model=AppConfig)


async def _user_row(config: Any, identifier: str) -> tuple[str, Mapping[str, Any]]:
    """Строка users пользователя стенда по логину: id и metadata входа.

    Пул свой и закрывается здесь же: общий пул процесса привязан к циклу
    событий того теста, который его открыл.
    """
    from psycopg import sql

    from boba.chat.threads import ChatTable
    from boba.db.postgres import AsyncPostgresPool, PgQueryBuilder

    pool = AsyncPostgresPool(config.postgres)
    await pool.open()
    try:
        query = (
            PgQueryBuilder(
                users=sql.Identifier(config.db_schema, ChatTable.USERS.value)
            )
            .add(
                "select id, meta from {users} where identifier = %(identifier)s",
                identifier=identifier,
            )
            .build()
        )
        async with pool.connection() as conn:
            cursor = await conn.execute(query.text, query.params)
            row = await cursor.fetchone()
    finally:
        await pool.close()

    if row is None:
        raise AssertionError(f"stand user {identifier!r} has no users row yet")

    return str(row[0]), dict(row[1])


async def _store(thread_id: str, files: Mapping[str, bytes], stand: E2eStand) -> None:
    """Кладёт файлы в workspace пользователя стенда на сервисе boba-mcp тем
    же путём, каким их кладёт чат: клиентом MCP от имени пользователя."""
    from boba.canvas.keys import ObjectKey
    from boba.identity.signin import SignInMetadata
    from boba.mcp_client.client import McpCaller, McpServers
    from boba.stand.storage import StorageSeed

    config = _app_config(stand)
    owner, meta = await _user_row(config, LOGIN[0])
    caller = McpCaller(login=LOGIN[0], roles=SignInMetadata.parse(meta).roles)
    server = config.profiles[FILES_PROFILE].mcp[0]
    servers = McpServers(config.mcp)
    try:
        remote = await servers.files(server, caller)
        if remote is None:
            raise AssertionError(f"mcp server {server!r} offers no file storage")

        for name, blob in files.items():
            key = ObjectKey(user_id=owner, scope_id=thread_id, name=name)
            await asyncio.wait_for(remote.upload(key, StorageSeed().chunks(blob)), 120)
    finally:
        await servers.stop()


async def _upload(thread_id: str, stand: E2eStand) -> None:
    await _store(thread_id, FILES, stand)


class _SessionProbe:
    """Слушатели страницы: sessionId и threadId из трафика, вердикты рендера.

    Сессию называет тело запроса socket.io, тред — кадр сокета после старта
    чата; joined ждёт обоих. Вердикты — payload действий canvas_render_status,
    которые браузер шлёт серверу после рендера диаграммы. socket — последний
    открытый страницей websocket."""

    JOIN_SEC: ClassVar[float] = 120.0
    STATUS_ACTION: ClassVar[str] = "canvas_render_status"

    def __init__(self) -> None:
        self.session: dict[str, str] = {}
        self.verdicts: list[Mapping[str, Any]] = []
        self.socket: Any = None
        self._joined = asyncio.Event()

    def watch(self, page: Page) -> None:
        page.on("request", self._on_request)
        page.on("websocket", self._on_websocket)

    async def joined(self) -> None:
        try:
            await asyncio.wait_for(self._joined.wait(), self.JOIN_SEC)
        except TimeoutError as exc:
            msg = (
                "chat page after sign-in: expected a sessionId and a threadId in "
                f"the socket traffic within {self.JOIN_SEC:.0f}s, got "
                f"{sorted(self.session)}"
            )
            raise AssertionError(msg) from exc

    def _on_request(self, request: Any) -> None:
        if request.method != "POST":
            return

        body = request.post_data
        if not body:
            return

        if request.url.endswith("/project/action"):
            action = json.loads(body)["action"]
            if action["name"] == self.STATUS_ACTION:
                self.verdicts.append(action["payload"])
            return

        if "socket.io" not in request.url:
            return

        found = re.search(r'"sessionId"\s*:\s*"([^"]+)"', body)
        if found:
            self.session["id"] = found.group(1)
            self._check()

    def _on_websocket(self, ws: Any) -> None:
        self.socket = ws
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, payload: Any) -> None:
        if isinstance(payload, bytes):
            return

        found = re.search(r'"threadId"\s*:\s*"([0-9a-f-]{36})"', payload)
        if found:
            self.session.setdefault("thread", found.group(1))
            self._check()

    def _check(self) -> None:
        if "id" not in self.session:
            return

        if "thread" not in self.session:
            return

        self._joined.set()


class PanelScript(StrEnum):
    """Скрипты страницы, по которым тест ждёт состояния панели канваса.

    Узел панели (CanvasView) несёт вид и метку показа — data-canvas-kind и
    data-canvas-nonce: по метке видно, что панель взялась за новый показ, по
    виду — что значит «вьювер его дорисовал». Метка null в DRAWN — «любой
    показ», и тогда ждётся только конец отрисовки."""

    NONCE = """() => {
        const panel = document.querySelector('[data-canvas-panel]');
        if (!panel) return '';
        return panel.dataset.canvasNonce;
    }"""
    DRAWN = """(previous) => {
        const panel = document.querySelector('[data-canvas-panel]');
        if (!panel) return false;
        if (panel.dataset.canvasNonce === previous) return false;
        const kind = panel.dataset.canvasKind;
        if (kind === 'mermaid') {
            return !!panel.querySelector(
                'div[style*="transform-origin"] :is(svg, pre)');
        }
        if (kind === 'image') {
            const img = panel.querySelector('img');
            return !!img && img.complete && img.naturalWidth > 0;
        }
        if (kind === 'pdf') {
            const layer = panel.querySelector('.react-pdf__Page .textLayer');
            return !!layer && layer.innerText.trim() !== '';
        }
        if (kind === 'video' || kind === 'audio') {
            return !!panel.querySelector('video, audio');
        }
        return panel.innerText.trim() !== '';
    }"""
    SHOWN = """() => {
        const panel = document.querySelector('[data-canvas-panel]');
        if (!panel) return 'no panel';
        const text = panel.innerText.replace(/\\s+/g, ' ').trim().slice(0, 300);
        return `${panel.dataset.canvasKind} show ${panel.dataset.canvasNonce}: ${text}`;
    }"""
    TEXT = """(text) => {
        const view = document.querySelector('#side-view-content');
        return !!view && view.innerText.includes(text);
    }"""
    IMAGE_LOADED = "(img) => img.complete && img.naturalWidth > 0"
    IMAGE_REVISED = """(before) => {
        const img = document.querySelector('#side-view-content img');
        return !!img && img.src !== before;
    }"""
    SVG = """() => {
        const svg = document.querySelector(
            '#side-view-content div[style*="transform-origin"] svg');
        if (!svg) return '';
        return svg.innerHTML;
    }"""
    SVG_CHANGED = """(before) => {
        const svg = document.querySelector(
            '#side-view-content div[style*="transform-origin"] svg');
        return !!svg && svg.innerHTML !== before;
    }"""
    SWITCH = """([path, mime]) => window.dispatchEvent(
        new CustomEvent('boba:canvas', {detail: {path, mime}}))"""
    EXPANDED = """() => {
        const stage = document.querySelector('[data-canvas-panel][data-full="true"]');
        if (!stage) return false;
        return stage.parentElement === (
            document.getElementById('root') || document.body);
    }"""
    COLLAPSED = """() => {
        const stage = document.querySelector('[data-canvas-panel][data-full="false"]');
        const side = document.querySelector('#side-view-content');
        if (!stage || !side) return false;
        return side.contains(stage);
    }"""
    CLOSED = "() => !document.querySelector('#side-view-content')"
    REMOUNTED = """(old) => !old.isConnected
        && !!document.querySelector('[data-canvas-panel]')"""
    GEOMETRY = """() => {
        const stage = document.querySelector('[data-canvas-panel][data-full="true"]');
        if (!stage) return null;
        const box = stage.getBoundingClientRect();
        return {
            width: box.width,
            height: box.height,
            left: box.left,
            top: box.top,
            winWidth: window.innerWidth,
            winHeight: window.innerHeight,
            hosted: stage.parentElement === (
                document.getElementById('root') || document.body),
            transformed: (() => {
                let node = stage.parentElement;
                while (node && node !== document.documentElement) {
                    if (getComputedStyle(node).transform !== 'none') return true;
                    node = node.parentElement;
                }
                return false;
            })(),
        };
    }"""
    WHEEL_ARMED = """() => {
        window.__bobaWheel = false;
        window.addEventListener('wheel', () => { window.__bobaWheel = true; },
            {once: true, passive: true});
    }"""
    WHEEL_SEEN = "() => window.__bobaWheel === true"
    SETTLED = """() => new Promise((done) =>
        requestAnimationFrame(() => requestAnimationFrame(() => done(true))))"""
    TRANSFORM = "(scene) => scene.style.transform"
    TRANSFORMED = """([selector, before]) => {
        const scene = document.querySelector(selector);
        return !!scene && scene.style.transform !== before;
    }"""
    PDF_TOOLBAR = """() => {
        const page = document.querySelector('#side-view-content .react-pdf__Page');
        const bar = document.querySelector(
            '#side-view-content [data-canvas-stage] .sticky');
        const canvas = document.querySelector(
            '#side-view-content .react-pdf__Page__canvas');
        const box = canvas.getBoundingClientRect();
        return {
            bar: bar.innerText.replace(/\\s+/g, ' ').trim(),
            opacity: getComputedStyle(bar).opacity,
            width: box.width, height: box.height,
            text: page.querySelector('.textLayer').innerText,
        };
    }"""
    PDF_BAR_CHANGED = """(before) => {
        const bar = document.querySelector(
            '#side-view-content [data-canvas-stage] .sticky');
        if (!bar) return false;
        return bar.innerText.replace(/\\s+/g, ' ').trim() !== before;
    }"""
    PDF_WIDER = """(width) => {
        const canvas = document.querySelector(
            '#side-view-content .react-pdf__Page__canvas');
        if (!canvas) return false;
        return canvas.getBoundingClientRect().width > width;
    }"""
    PDF_FITS = """(width) => {
        if (window.innerWidth !== width) return false;
        const side = document.querySelector('#side-view-content');
        if (!side) return false;
        const layer = side.querySelector('.react-pdf__Page .textLayer');
        if (!layer || layer.innerText.trim() === '') return false;
        return side.scrollWidth - side.clientWidth <= 2;
    }"""


class CanvasPage:
    """Страница чата с живым тредом: показ файлов в панели канваса и
    ожидание состояния, которого добивается действие.

    Создаётся фикстурой panel после входа и загрузки файлов треда; тесты
    зовут его методы. Каждое действие ждёт состояния DOM, которое должно
    дать: панель взялась за новый показ и вьювер его дорисовал, сцена
    развернулась на весь экран или вернулась в панель, — а не фиксированной
    паузы: под нагрузкой прогона отрисовка растягивается на секунды. Срок
    ожидания ловит зависание, а не медленную отрисовку.
    """

    TIMEOUT_MS: ClassVar[float] = 60000.0
    SIDE: ClassVar[str] = "#side-view-content"
    ACTION_PATH: ClassVar[str] = "/project/action"
    LEAVE_ACTION: ClassVar[str] = "canvas_leave"
    NARROW_WIDTH: ClassVar[int] = 420
    """Окно уже точки перелома chainlit (768 px): панель — лист поверх чата."""

    def __init__(self, page: Page, probe: _SessionProbe, stand: E2eStand) -> None:
        self.page = page
        self.probe = probe
        self._stand = stand
        self.thread = probe.session["thread"]
        self._session_id = probe.session["id"]

    def tool_view(self, name: str) -> str:
        """Путь файла глазами тела инструмента: workspace сервиса boba-mcp."""
        return f"/workspace/{self.thread}/upload/{name}"

    async def rewrite(self, name: str, blob: bytes) -> None:
        """Переписать файл workspace: так его меняет инструмент между показами."""
        await _store(self.thread, {name: blob}, self._stand)

    async def show(self, name: str) -> Locator:
        """Клик по ссылке на файл: ссылка называет и путь, и вид файла."""
        payload = {"path": self.tool_view(name), "mime": ContentMime.of_name(name)}

        return await self.act("canvas_open", payload)

    async def act(self, name: str, payload: Mapping[str, str]) -> Locator:
        """Действие панели от имени вкладки; возвращает панель, когда она
        дорисовала показ, открытый действием."""
        previous = await self.page.evaluate(PanelScript.NONCE)
        body = {
            "sessionId": self._session_id,
            "action": {
                "name": name,
                "payload": dict(payload),
                "label": "",
                "tooltip": "",
                "icon": None,
                "forId": None,
                "id": "e2e",
            },
        }
        url = BASE + self.ACTION_PATH
        # cookie входа может быть Secure (SameSite=None в конфиге): по http
        # API-контекст Playwright её не отправит, поэтому заголовок явный
        answer = await self.page.request.post(
            url,
            data=json.dumps(body),
            headers={
                "content-type": "application/json",
                "cookie": await _cookie_header(self.page.context),
            },
        )
        if not answer.ok:
            text = await answer.text()
            raise AssertionError(
                f"POST {url} {name} {dict(payload)}: expected 200, got "
                f"{answer.status}: {text[:200]}"
            )

        await self.until(PanelScript.DRAWN, previous, f"a new show drawn after {name}")
        return self.page.locator(self.SIDE)

    async def switch(self, name: str) -> None:
        """Смена файла в открытой панели событием boba:canvas — так её меняет
        ссылка в ленте: описание приходит ответом, элемент не подменяется."""
        previous = await self.page.evaluate(PanelScript.NONCE)
        path = self.tool_view(name)

        await self.page.evaluate(PanelScript.SWITCH, [path, ContentMime.of_name(name)])
        await self.until(PanelScript.DRAWN, previous, f"{path} drawn after a switch")

    async def relayout(self, width: int) -> None:
        """Окно шириной width по ту сторону точки перелома: chainlit меняет
        раскладку (лист узкого окна, боковая панель широкого) и монтирует
        элемент панели заново. Возвращается, когда прежний узел панели ушёл
        со страницы, сервер ответил на уход прежнего экземпляра с показа и
        новый экземпляр дорисовал показ."""
        size = self.page.viewport_size
        if size is None:
            raise AssertionError("the page has no viewport size")

        old = await self.page.locator("[data-canvas-panel]").element_handle()
        async with self.page.expect_request_finished(
            self._is_leave, timeout=self.TIMEOUT_MS
        ):
            await self.page.set_viewport_size(
                {"width": width, "height": size["height"]}
            )
            await self.until(
                PanelScript.REMOUNTED, old, f"the panel mounted anew at {width}px"
            )

        await self.until(PanelScript.DRAWN, None, f"the show drawn at {width}px")

    def _is_leave(self, request: Any) -> bool:
        if not request.url.endswith(self.ACTION_PATH):
            return False

        body = request.post_data
        if not body:
            return False

        return json.loads(body)["action"]["name"] == self.LEAVE_ACTION

    async def expand(self) -> Any:
        """На весь экран кнопкой шапки; отдаёт геометрию развёрнутой сцены."""
        button = self.page.locator('button[data-canvas-action="fullscreen"]')

        await button.first.click()
        await self.until(PanelScript.EXPANDED, None, "the scene expanded into the root")
        return await self.page.evaluate(PanelScript.GEOMETRY)

    async def collapse(self) -> None:
        """Escape сворачивает полный экран: сцена возвращается в панель."""
        await self.page.keyboard.press("Escape")
        await self.until(PanelScript.COLLAPSED, None, "the scene back in the panel")

    async def close(self) -> None:
        """Кнопка закрытия в ряду шапки: панель уходит со страницы."""
        button = self.page.locator(f'{self.SIDE} button[data-canvas-action="close"]')

        await button.first.click()
        await self.until(PanelScript.CLOSED, None, "the panel closed")

    async def wheel(self, delta_y: float) -> None:
        """Колесо под курсором; возвращается, когда событие дошло до окна и
        страница отрисовала кадр после его обработчиков."""
        await self.page.evaluate(PanelScript.WHEEL_ARMED)
        await self.page.mouse.wheel(0, delta_y)
        await self.until(PanelScript.WHEEL_SEEN, None, "the wheel event on the window")
        await self.page.evaluate(PanelScript.SETTLED)

    async def transformed(self, selector: str, before: str) -> None:
        """Ждёт, пока сцена selector сменит transform с before."""
        await self.until(
            PanelScript.TRANSFORMED,
            [selector, before],
            f"the transform of {selector} to change from {before!r}",
        )

    async def await_text(self, text: str) -> None:
        """Ждёт, пока панель покажет строку text."""
        await self.until(PanelScript.TEXT, text, f"the text {text!r} in the panel")

    async def pdf_state(self) -> Any:
        """Панель pdf.js в сцене: счётчики, видимость, холст, текстовый слой."""
        return await self.page.evaluate(PanelScript.PDF_TOOLBAR)

    async def pdf_click(self, icon: str) -> None:
        """Кнопка панели pdf.js по её значку lucide; возвращается, когда
        панель pdf.js показала итог нажатия — другую страницу или масштаб."""
        before = (await self.pdf_state())["bar"]
        button = self.page.locator(
            f"{self.SIDE} [data-canvas-stage] button:has(svg.lucide-{icon})"
        )

        await button.click()
        await self.until(
            PanelScript.PDF_BAR_CHANGED,
            before,
            f"the pdf toolbar to leave {before!r} after {icon}",
        )

    async def until(self, script: str, arg: object, what: str) -> None:
        """Ждёт, пока предикат страницы script(arg) станет истинным."""
        try:
            await self.page.wait_for_function(script, arg=arg, timeout=self.TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            shown = await self.page.evaluate(PanelScript.SHOWN)
            raise AssertionError(
                f"canvas panel: expected {what} within "
                f"{self.TIMEOUT_MS / 1000:.0f}s, the panel shows {shown}"
            ) from exc


@pytest.fixture(scope="module")
async def panel(app_server: E2eProcesses) -> AsyncIterator[CanvasPage]:
    """Логин, живой тред и файлы в нём; отдаёт страницу с панелью канваса."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(viewport={"width": 1600, "height": 900})
        page = await context.new_page()

        probe = _SessionProbe()
        probe.watch(page)

        await page.goto(BASE + "/login")
        await page.wait_for_selector("input")
        inputs = page.locator("form input")
        await inputs.nth(0).fill(LOGIN[0])
        await inputs.nth(1).fill(LOGIN[1])
        await page.locator('form button[type="submit"]').first.click()

        await probe.joined()
        await _upload(probe.session["thread"], app_server.stand)

        yield CanvasPage(page, probe, app_server.stand)

        await browser.close()


async def test_image_is_rendered(panel: CanvasPage) -> None:
    """Картинка от bash/python-тула должна реально загрузиться, а не быть битой.

    Показ картинки дорисован, когда img загружен: show ждёт этого сам.
    """
    side = await panel.show("chart.png")

    loaded = await side.locator("img").evaluate(PanelScript.IMAGE_LOADED)
    if loaded is not True:
        raise AssertionError("the image of the panel is broken")


async def test_svg_is_rendered(panel: CanvasPage) -> None:
    side = await panel.show("chart.svg")

    loaded = await side.locator("img").evaluate(PanelScript.IMAGE_LOADED)
    if loaded is not True:
        raise AssertionError("the svg of the panel is broken")


async def test_diagram_is_drawn_not_shown_as_text(panel: CanvasPage) -> None:
    """Спека mermaid превращается в svg, а не показывается текстом файла."""
    side = await panel.show("flow.mmd")
    text = await side.inner_text()

    if await side.locator('div[style*="transform-origin"] svg').count() <= 0:
        raise AssertionError(
            "await side.locator('div[style*=\"transform-origin\"] svg'…"
        )
    if "Доход" not in text:
        raise AssertionError('"Доход" in text')
    # имя файла в панели не дублируем: шапка отнимала бы место у диаграммы
    if "flow.mmd" in text:
        raise AssertionError('"flow.mmd" not in text')


async def test_close_button_does_not_cover_the_diagram(panel: CanvasPage) -> None:
    """В canvas-режиме chainlit красит кнопку закрытия в primary; css её гасит."""
    await panel.show("flow.mmd")
    close = panel.page.locator("#side-view-title button").first

    style = await close.evaluate(
        "el => { const s = getComputedStyle(el);"
        " return {bg: s.backgroundColor, opacity: s.opacity}; }"
    )

    if style["bg"] not in ("rgba(0, 0, 0, 0)", "transparent"):
        raise AssertionError('style["bg"] in ("rgba(0, 0, 0, 0)", "transparent")')
    if float(style["opacity"]) >= 1:
        raise AssertionError('float(style["opacity"]) < 1')


async def test_markdown_is_rendered(panel: CanvasPage) -> None:
    side = await panel.show("report.md")

    if "Отчёт" not in await side.inner_text():
        raise AssertionError('"Отчёт" in await side.inner_text()')


async def test_unsupported_format_is_explained(panel: CanvasPage) -> None:
    """Неподдерживаемый формат: панель объясняет, а не молчит и не врёт.

    Имени файла в панели нет намеренно: шапки у канваса убраны, объяснение
    живёт по центру пустой панели.
    """
    side = await panel.show("data.bin")
    text = await side.inner_text()

    if E2eTexts().head(CanvasNote.UNSUPPORTED.value) not in text:
        raise AssertionError(f"the unsupported format is not explained: {text!r}")


async def test_broken_spec_verdict_reaches_server(panel: CanvasPage) -> None:
    """Ошибку синтаксиса видит только браузер: плашка в панели, вердикт — серверу.

    Вердикт берётся по метке именно этого показа: сервер ждёт его, прежде
    чем ответить на действие, поэтому к концу show он уже отправлен.
    """
    side = await panel.show("broken.mmd")
    text = await side.inner_text()
    nonce = await panel.page.evaluate(PanelScript.NONCE)

    if E2eTexts().head(E2eTexts.DIAGRAM_FAILED) not in text:
        raise AssertionError(f"the diagram failure is not shown: {text!r}")
    if "Parse error" not in text:
        raise AssertionError('"Parse error" in text')

    verdicts: list[Mapping[str, Any]] = []
    for verdict in panel.probe.verdicts:
        if verdict["nonce"] == nonce:
            verdicts.append(verdict)

    if len(verdicts) != 1:
        raise AssertionError(f"expected one render verdict of show {nonce}: {verdicts}")
    if verdicts[0]["ok"] is not False:
        raise AssertionError(f"the browser reported a broken spec as drawn: {verdicts}")
    if "Parse error" not in verdicts[0]["error"]:
        raise AssertionError(f"the verdict carries no mermaid error: {verdicts}")


async def test_diagram_fills_the_panel(panel: CanvasPage) -> None:
    """Диаграмма занимает всю панель: mermaid отдаёт svg в своих пикселях."""
    side = await panel.show("flow.mmd")

    box = await side.bounding_box()
    svg = await side.locator('div[style*="transform-origin"] svg').first.bounding_box()

    if not (box):
        raise AssertionError("box")
    if not (svg):
        raise AssertionError("svg")
    if svg["width"] <= box["width"] * 0.9:
        raise AssertionError('svg["width"] > box["width"] * 0.9')
    if svg["height"] <= box["height"] * 0.8:
        raise AssertionError('svg["height"] > box["height"] * 0.8')


async def test_wheel_does_not_zoom_in_the_panel(panel: CanvasPage) -> None:
    """Колесо в панели листает страницу; зум остаётся полноэкранному режиму."""
    side = await panel.show("flow.mmd")
    scene = side.locator('div[style*="transform-origin"]')
    page = panel.page

    before = await scene.evaluate(PanelScript.TRANSFORM)
    # наводим в центр: сверху висит кнопка закрытия панели
    box = await side.bounding_box()
    if not (box):
        raise AssertionError("box")
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await panel.wheel(-400)

    after = await scene.evaluate(PanelScript.TRANSFORM)
    if after != before:
        raise AssertionError(f"the wheel zoomed the panel: {before!r} -> {after!r}")


async def test_switching_file_does_not_reopen_the_panel(panel: CanvasPage) -> None:
    """Открытая панель меняет содержимое сама: анимация открытия не повторяется."""
    side = await panel.show("flow.mmd")
    page = panel.page

    await page.evaluate(
        """() => {
            const content = document.querySelector('#side-view-content');
            const node = content.closest('[class*="translate-x-"]');
            window.__reopens = 0;
            new MutationObserver(() => { window.__reopens += 1; })
              .observe(node, {attributes: true, attributeFilter: ['class']});
            window.__panelNode = node;
        }"""
    )

    await panel.switch("report.md")

    if "Отчёт" not in await side.inner_text():
        raise AssertionError('"Отчёт" in await side.inner_text()')

    same_node = await page.evaluate(
        "() => window.__panelNode === document.querySelector('#side-view-content')"
        ".closest('[class*=translate-x-]')"
    )

    if not (same_node):
        raise AssertionError(
            "панель пересоздалась — анимация открытия проиграется заново"
        )
    if await page.evaluate("() => window.__reopens") != 0:
        raise AssertionError('await page.evaluate("() => window.__reopens") == 0')


async def test_controls_share_one_alignment(panel: CanvasPage) -> None:
    """Кнопки шапки — один ряд по стандарту chainlit: 36px, закрытие справа.

    Родной «назад» панели спрятан: закрытие живёт последней кнопкой ряда,
    как в полноэкранном режиме.
    """
    await panel.show("flow.mmd")

    geometry = await panel.page.evaluate(
        """() => {
            const panel = document.querySelector('#side-view-content')
              .closest('.bg-card').getBoundingClientRect();
            const back = document.querySelector('#side-view-title');
            const buttons = [...document.querySelectorAll(
              '#side-view-content button[data-canvas-action]'
            )].map(b => {
              const r = b.getBoundingClientRect();
              return {
                label: b.getAttribute('data-canvas-action'),
                top: Math.round(r.top - panel.top),
                right: Math.round(panel.right - r.right),
                size: Math.round(r.width),
              };
            });
            return {
              backHidden: !back || getComputedStyle(back).display === 'none',
              buttons,
            };
        }"""
    )

    if geometry["backHidden"] is not True:
        raise AssertionError('geometry["backHidden"] is True')
    if not (geometry["buttons"]):
        raise AssertionError('geometry["buttons"]')
    tops = {entry["top"] for entry in geometry["buttons"]}
    if max(tops) - min(tops) > 2:
        raise AssertionError("max(tops) - min(tops) <= 2")
    for entry in geometry["buttons"]:
        if entry["size"] != 36:
            raise AssertionError('entry["size"] == 36')

    last = geometry["buttons"][-1]
    if last["label"] != "close":
        raise AssertionError('last["label"] == "close"')
    if abs(last["right"] - 25) > 4:
        raise AssertionError('abs(last["right"] - 25) <= 4')


async def test_fullscreen_controls_are_on_the_close_line(panel: CanvasPage) -> None:
    """В полноэкранном режиме зум стоит на одной линии с кнопкой закрытия.

    Полный экран — CSS-оверлей той же сцены (data-full), а не отдельный
    диалог: DOM не пересоздаётся при разворачивании.
    """
    await panel.show("flow.mmd")
    page = panel.page

    await panel.expand()

    rows = await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-panel][data-full="true"]');
            const box = stage.getBoundingClientRect();
            return [...stage.querySelectorAll('button')]
              .filter(b => getComputedStyle(b).display !== 'none')
              .map(b => {
                const r = b.getBoundingClientRect();
                return Math.round(r.top - box.top);
              });
        }"""
    )
    await panel.collapse()

    if not (rows):
        raise AssertionError("rows")
    if max(rows) - min(rows) > 2:
        raise AssertionError("max(rows) - min(rows) <= 2")


async def test_broken_diagram_keeps_the_control_row(panel: CanvasPage) -> None:
    """Панель со сломанной диаграммой — та же панель: ряд кнопок обязан быть.

    Раньше аварийные ветки вьюверов рисовались мимо рамки, и пользователь
    оставался без зума, полноэкранного режима и закрытия.
    """
    await panel.show("broken.mmd")

    labels = await panel.page.evaluate(
        """() => [...document.querySelectorAll(
            '#side-view-content button[data-canvas-action]'
        )].map(b => b.getAttribute('data-canvas-action'))"""
    )

    if labels[-2:] != ["fullscreen", "close"]:
        raise AssertionError('labels[-2:] == ["fullscreen", "close"]')
    if "zoom-in" not in labels:
        raise AssertionError('"zoom-in" in labels')
    if "zoom-out" not in labels:
        raise AssertionError('"zoom-out" in labels')
    if "reset-view" not in labels:
        raise AssertionError('"reset-view" in labels')


async def test_unsupported_format_keeps_the_control_row(panel: CanvasPage) -> None:
    """Объяснение «формат не показать» тоже живёт в общей рамке."""
    await panel.show("data.bin")

    labels = await panel.page.evaluate(
        """() => [...document.querySelectorAll(
            '#side-view-content button[data-canvas-action]'
        )].map(b => b.getAttribute('data-canvas-action'))"""
    )

    if labels[-2:] != ["fullscreen", "close"]:
        raise AssertionError('labels[-2:] == ["fullscreen", "close"]')


async def test_file_link_does_not_depend_on_the_session(panel: CanvasPage) -> None:
    """Ссылка панели адресует файл, а не запись в памяти сессии.

    Сессионная ссылка умирала вместе с сокетом (перезапуск приложения,
    переподключение вкладки), и картинка молча превращалась в битый img.
    """
    side = await panel.show("chart.svg")

    src = await side.locator("img").evaluate("img => img.src")

    if "session_id" in src:
        raise AssertionError('"session_id" not in src')
    if f"/canvas/{panel.thread}/upload/chart.svg" not in src:
        raise AssertionError(f"the link does not address the file: {src}")


async def test_panel_switches_after_a_render_error(panel: CanvasPage) -> None:
    """Сломанная диаграмма не должна запирать панель: следующий файл рисуется."""
    side = await panel.show("broken.mmd")

    shown = await side.inner_text()
    if E2eTexts().head(E2eTexts.DIAGRAM_FAILED) not in shown:
        raise AssertionError(f"the diagram failure is not shown: {shown!r}")

    await panel.switch("flow.mmd")

    text = await side.inner_text()
    if "Доход" not in text:
        raise AssertionError('"Доход" in text')
    if E2eTexts().head(E2eTexts.DIAGRAM_FAILED) in text:
        raise AssertionError(f"the diagram is shown as failed: {text!r}")


async def test_switch_to_the_same_spec_draws_it(panel: CanvasPage) -> None:
    """Смена на диаграмму с той же спекой рисует её заново: у нового показа
    свой вьювер, и svg прошлого показа не сбрасывается в вечное «рисуется»."""
    side = await panel.show("flow.mmd")

    await panel.switch("flow-copy.mmd")

    if await side.locator('div[style*="transform-origin"] svg').count() != 1:
        raise AssertionError("the copy of the diagram is not drawn into an svg")


async def test_stream_channel_delivers_to_the_panel(panel: CanvasPage) -> None:
    """Канал живого вывода доезжает до DOM панели.

    Ход агента в e2e не гоняется, поэтому потока нет — но объяснение
    «поток недоступен» идёт в панель тем же каналом, каким насос шлёт
    снапшоты окна: доставка action -> канал -> сокет -> DOM проверяется
    без инструмента.
    """
    side = await panel.act("canvas_stream", {"call_id": "e2e-нет-такого-вызова"})

    shown = await side.inner_text()
    if E2eTexts().head(CanvasNote.GONE.value) not in shown:
        raise AssertionError(f"the missing journal is not explained: {shown!r}")


async def test_panel_close_button_closes_the_panel(panel: CanvasPage) -> None:
    """Закрытие в ряду кнопок реально закрывает панель канваса: close ждёт,
    пока панель уйдёт со страницы, и падает, если она осталась."""
    await panel.show("report.md")

    await panel.close()


@pytest.mark.parametrize("name", ["report.md", "report.pdf"])
async def test_bar_survives_scrolling(panel: CanvasPage, name: str) -> None:
    """Прокрутка содержимого не уводит шапку: кнопки доступны всегда."""
    await panel.show(name)
    page = panel.page

    before = await page.evaluate(
        """() => document.querySelector(
            '#side-view-content button[data-canvas-action]'
        ).getBoundingClientRect().top"""
    )

    await page.evaluate(
        """() => {
            for (const el of document.querySelectorAll(
              '#side-view-content div'
            )) {
              if (el.scrollHeight > el.clientHeight) el.scrollTop = 99999;
            }
        }"""
    )
    await page.evaluate(PanelScript.SETTLED)

    after = await page.evaluate(
        """() => {
            const button = document.querySelector(
              '#side-view-content button[data-canvas-action]'
            );
            const r = button.getBoundingClientRect();
            return { top: r.top, height: r.height };
        }"""
    )

    if after["height"] <= 0:
        raise AssertionError('after["height"] > 0')
    if abs(after["top"] - before) > 2:
        raise AssertionError('abs(after["top"] - before) <= 2')


async def test_diagram_redraws_in_place_when_the_file_changes(
    panel: CanvasPage,
) -> None:
    """Файл спеки переписан — диаграмма перерисовалась на месте.

    Содержимое не едет пушем: слежение шлёт сигнал, фронт сам забирает
    свежую спеку и рендерит её в той же панели — штамп на DOM переживает
    обновление, панель не переоткрывается.
    """
    side = await panel.show("flow.mmd")
    page = panel.page

    if "Доход" not in await side.inner_text():
        raise AssertionError('"Доход" in await side.inner_text()')

    await page.evaluate(
        """() => {
            const stage = document.querySelector(
              '#side-view-content [data-canvas-stage]');
            stage.dataset.stamp = 'alive';
        }"""
    )

    await panel.rewrite(
        "flow.mmd", "flowchart LR\n    A[Доход] --> C[Прибыль]\n".encode()
    )
    await panel.await_text("Прибыль")

    stamp = await page.evaluate(
        """() => document.querySelector(
            '#side-view-content [data-canvas-stage]'
        ).dataset.stamp || ''"""
    )
    if stamp != "alive":
        raise AssertionError("панель пересоздалась при обновлении диаграммы")


async def test_zoom_buttons_scale_the_diagram(panel: CanvasPage) -> None:
    """Кнопки зума меняют масштаб сцены; сброс возвращает исходный вид.

    Клик — дискретное событие: React применяет его состояние до того, как
    клик вернётся, поэтому масштаб читается сразу.
    """
    side = await panel.show("flow.mmd")
    scene = side.locator('div[style*="transform-origin"]')

    base = await scene.evaluate(PanelScript.TRANSFORM)
    await side.locator('button[data-canvas-action="zoom-in"]').click()
    zoomed = await scene.evaluate(PanelScript.TRANSFORM)
    await side.locator('button[data-canvas-action="reset-view"]').click()
    reset = await scene.evaluate(PanelScript.TRANSFORM)

    if "scale(1)" not in base:
        raise AssertionError('"scale(1)" in base')
    if "scale(1.25)" not in zoomed:
        raise AssertionError('"scale(1.25)" in zoomed')
    if "scale(1)" not in reset:
        raise AssertionError('"scale(1)" in reset')


def _assert_covers_window(geometry: Any, label: str) -> None:
    """Сцена обязана занимать всё окно, а не рамку панели."""
    if geometry is None:
        raise AssertionError(f"{label}: полноэкранная сцена не найдена")
    if geometry["width"] < geometry["winWidth"] * 0.98:
        raise AssertionError(
            f"{label}: ширина {geometry['width']} вместо {geometry['winWidth']}"
        )
    if geometry["height"] < geometry["winHeight"] * 0.98:
        raise AssertionError(
            f"{label}: высота {geometry['height']} вместо {geometry['winHeight']}"
        )
    if abs(geometry["left"]) > 2 or abs(geometry["top"]) > 2:
        raise AssertionError(
            f"{label}: сцена смещена в ({geometry['left']}, {geometry['top']})"
        )


async def test_fullscreen_covers_the_whole_window(panel: CanvasPage) -> None:
    """Полный экран занимает окно целиком, а не рамку панели.

    Панель chainlit анимируется трансформацией, а position:fixed внутри
    трансформированного предка меряется от него — поэтому сцена уезжает в
    body. Проверяем реальную геометрию, а не атрибут разворачивания.
    """
    await panel.show("flow.mmd")

    geometry = await panel.expand()
    await panel.collapse()

    _assert_covers_window(geometry, "диаграмма")
    if geometry["hosted"] is not True:
        raise AssertionError("сцена поднята не в корень приложения")
    if geometry["transformed"] is not False:
        raise AssertionError("у сцены остался трансформированный предок: fixed съедет")


async def test_fullscreen_covers_the_window_for_every_viewer(panel: CanvasPage) -> None:
    """Каждый вьювер разворачивается на всё окно: рамка одна на все типы."""
    for name in ("chart.png", "report.md", "data.bin", "notes.log", "report.pdf"):
        await panel.show(name)
        geometry = await panel.expand()
        await panel.collapse()

        _assert_covers_window(geometry, name)


async def test_collapse_returns_the_scene_into_the_panel(panel: CanvasPage) -> None:
    """Сворачивание возвращает узел в панель: содержимое снова внутри неё."""
    await panel.show("flow.mmd")

    await panel.expand()
    await panel.collapse()

    placement = await panel.page.evaluate(
        """() => {
            const stage = document.querySelector('[data-canvas-panel]');
            const panel = document.querySelector('#side-view-content');
            return {
                full: stage.getAttribute('data-full'),
                inPanel: panel.contains(stage),
                strays: document.querySelectorAll(
                    'body > [data-canvas-panel], #root > [data-canvas-panel]'
                ).length,
            };
        }"""
    )

    if placement["full"] != "false":
        raise AssertionError("сцена осталась развёрнутой")
    if placement["inPanel"] is not True:
        raise AssertionError("сцена не вернулась в панель")
    if placement["strays"] != 0:
        raise AssertionError("в корне остался осиротевший узел сцены")


async def test_zoom_survives_expanding(panel: CanvasPage) -> None:
    """Разворачивание не сбрасывает состояние вьювера: масштаб переживает его."""
    side = await panel.show("flow.mmd")
    scene = panel.page.locator('[data-canvas-stage] div[style*="transform-origin"]')

    await side.locator('button[data-canvas-action="zoom-in"]').first.click()
    before = await scene.evaluate(PanelScript.TRANSFORM)

    await panel.expand()
    expanded = await scene.evaluate(PanelScript.TRANSFORM)
    await panel.collapse()
    collapsed = await scene.evaluate(PanelScript.TRANSFORM)

    if "scale(1.25)" not in before:
        raise AssertionError('"scale(1.25)" in before')
    if expanded != before:
        raise AssertionError("масштаб сбросился при разворачивании")
    if collapsed != before:
        raise AssertionError("масштаб сбросился при сворачивании")


async def test_panel_never_scrolls_horizontally(panel: CanvasPage) -> None:
    """Содержимое не выталкивает панель вбок: горизонтальной прокрутки нет."""
    for name in ("flow.mmd", "report.md", "chart.png", "report.pdf"):
        await panel.show(name)
        overflow = await panel.page.evaluate(
            """() => {
                const panel = document.querySelector('#side-view-content');
                return panel.scrollWidth - panel.clientWidth;
            }"""
        )

        if overflow > 2:
            raise AssertionError(f"{name}: панель прокручивается вбок на {overflow}px")


# ——— Вид: слои, фон, раскладка ———


async def test_fullscreen_covers_the_chat_underneath(panel: CanvasPage) -> None:
    """Полный экран перекрывает ленту: клик в центре попадает в сцену."""
    await panel.show("flow.mmd")

    await panel.expand()
    hit = await panel.page.evaluate(
        """() => {
            const stage = document.querySelector(
              '[data-canvas-panel][data-full="true"]');
            const found = document.elementFromPoint(
                window.innerWidth / 2, window.innerHeight / 2);
            const style = getComputedStyle(stage);
            return {
                inside: stage.contains(found),
                background: style.backgroundColor,
                zIndex: style.zIndex,
            };
        }"""
    )
    await panel.collapse()

    if hit["inside"] is not True:
        raise AssertionError("лента перекрывает полноэкранную сцену")
    if hit["background"] in ("rgba(0, 0, 0, 0)", "transparent"):
        raise AssertionError("фон полноэкранной сцены прозрачный")
    if int(hit["zIndex"]) < 10:
        raise AssertionError(f"z-index сцены слишком мал: {hit['zIndex']}")


async def test_fullscreen_body_fills_the_height(panel: CanvasPage) -> None:
    """Тело занимает всё под шапкой: содержимое не жмётся в верхнюю полосу."""
    await panel.show("report.md")

    await panel.expand()
    layout = await panel.page.evaluate(
        """() => {
            // шапку узнаём по строке статуса, тело — следующий за ней блок
            const status = document.querySelector(
              '[data-canvas-panel][data-full="true"] [data-canvas-status]');
            const bar = status.parentElement;
            const body = bar.nextElementSibling;
            const barBox = bar.getBoundingClientRect();
            const bodyBox = body.getBoundingClientRect();
            return {
                barHeight: barBox.height,
                bodyHeight: bodyBox.height,
                bodyTop: Math.round(bodyBox.top - barBox.bottom),
                winHeight: window.innerHeight,
            };
        }"""
    )
    await panel.collapse()

    if layout["barHeight"] <= 0:
        raise AssertionError("шапка не видна в полноэкранном режиме")
    if abs(layout["bodyTop"]) > 2:
        raise AssertionError("тело не примыкает к шапке")
    expected = layout["winHeight"] - layout["barHeight"]
    if layout["bodyHeight"] < expected * 0.95:
        raise AssertionError("тело не занимает высоту под шапкой")


async def test_image_fits_without_overflow(panel: CanvasPage) -> None:
    """Картинка вписана в панель, а не торчит за её границами."""
    await panel.show("chart.png")

    fits = await panel.page.evaluate(
        """() => {
            const img = document.querySelector('#side-view-content img');
            const box = img.getBoundingClientRect();
            const panel = document.querySelector(
              '#side-view-content').getBoundingClientRect();
            return box.width <= panel.width + 2 && box.height <= panel.height + 2;
        }"""
    )

    if fits is not True:
        raise AssertionError("картинка выходит за границы панели")


async def test_log_is_monospaced(panel: CanvasPage) -> None:
    """Журнал показывается моноширинным: колонки лога не разъезжаются."""
    await panel.show("notes.log")

    family = await panel.page.evaluate(
        """() => getComputedStyle(
            document.querySelector('#side-view-content pre')).fontFamily"""
    )

    if "mono" not in family.lower():
        raise AssertionError(f"шрифт журнала не моноширинный: {family}")


# ——— Поведение: обновление файлов по сигналу слежения ———


async def test_markdown_reloads_when_the_file_changes(panel: CanvasPage) -> None:
    """Переписанный markdown перечитывается панелью без её переоткрытия."""
    await panel.show("report.md")
    page = panel.page

    await page.evaluate(
        """() => {
            document.querySelector(
              '#side-view-content [data-canvas-stage]').dataset.stamp = 'alive';
        }"""
    )

    await panel.rewrite("report.md", "# Отчёт\n\n- прибыль: 1 234\n".encode())
    await panel.await_text("прибыль")

    stamp = await page.evaluate(
        """() => document.querySelector(
            '#side-view-content [data-canvas-stage]').dataset.stamp || ''"""
    )

    if stamp != "alive":
        raise AssertionError("панель пересоздалась при обновлении markdown")


async def test_image_reloads_when_the_file_changes(panel: CanvasPage) -> None:
    """Перерисованная картинка подтягивается: ссылка получает метку ревизии."""
    side = await panel.show("chart.png")
    before = await side.locator("img").evaluate("img => img.src")

    # тот же валидный PNG: меняется только время записи, и этого довольно —
    # версия файла считается по размеру вместе с ним
    await panel.rewrite("chart.png", PNG)
    await panel.until(
        PanelScript.IMAGE_REVISED, before, "the image link to get a new revision"
    )

    # смена src запускает загрузку заново: ждём её, а не спрашиваем мгновенно
    await panel.until(PanelScript.DRAWN, None, "the revised image loaded")


async def test_file_after_a_notice_follows_its_changes(panel: CanvasPage) -> None:
    """Файл, показанный после объяснения, панель продолжает отслеживать.

    Уход с объяснения не снимает слежение следующего показа: у объяснения
    своя метка, и canvas_leave с ней чужое слежение не трогает. Картинка своя
    и залита на старте модуля: версия файла — размер и секунда записи, и
    перезапись тем же PNG меняет её, только если прошлая запись была раньше.
    """
    await panel.show("data.bin")
    side = await panel.show("after-notice.png")
    before = await side.locator("img").evaluate("img => img.src")

    await panel.rewrite("after-notice.png", PNG)
    await panel.until(
        PanelScript.IMAGE_REVISED, before, "the image link to get a new revision"
    )


async def test_panel_follows_the_file_across_layouts(panel: CanvasPage) -> None:
    """Смена раскладки монтирует элемент панели заново, а показ тот же:
    новый экземпляр держит слежение своей меткой, и правка файла видна и в
    листе узкого окна, и после возврата к боковой панели. Уход прежнего
    экземпляра сервер принимает раньше правки — снять слежение он должен
    был бы именно тогда. Правки разной длины: версия файла — размер и
    секунда записи."""
    wide = panel.page.viewport_size
    if wide is None:
        raise AssertionError("the page has no viewport size")

    side = await panel.show("layout.png")
    try:
        for step, width in enumerate((panel.NARROW_WIDTH, wide["width"]), start=1):
            await panel.relayout(width)
            before = await side.locator("img").evaluate("img => img.src")

            await panel.rewrite("layout.png", PNG + bytes(step))
            await panel.until(
                PanelScript.IMAGE_REVISED,
                before,
                f"the image link to get a new revision at {width}px",
            )
    finally:
        await panel.page.set_viewport_size(wide)


async def test_switched_file_survives_a_layout_change(panel: CanvasPage) -> None:
    """Панель, сменившая файл на месте, после смены раскладки показывает
    тот же файл, а не тот, с которым её открыл пуш элемента."""
    wide = panel.page.viewport_size
    if wide is None:
        raise AssertionError("the page has no viewport size")

    await panel.show("chart.png")
    await panel.switch("report.md")
    switched = await panel.page.evaluate(PanelScript.NONCE)
    try:
        await panel.relayout(panel.NARROW_WIDTH)
        shown = await panel.page.evaluate(PanelScript.NONCE)
        described = await panel.page.evaluate(PanelScript.SHOWN)
    finally:
        await panel.page.set_viewport_size(wide)

    if shown != switched:
        raise AssertionError(
            f"after the layout change the panel left show {switched} of report.md: "
            f"{described}"
        )


async def test_panel_follows_the_file_after_the_socket_reconnects(
    panel: CanvasPage, app_server: E2eProcesses
) -> None:
    """Вкладка без сети теряет сокет, и сторож треда без подключённых
    вкладок кончается; вернувшийся сокет снова объявляет показ, и правка
    файла после переподключения доходит до панели."""
    side = await panel.show("reconnect.png")
    before = await side.locator("img").evaluate("img => img.src")
    context = panel.page.context
    socket = panel.probe.socket
    path = panel.tool_view("reconnect.png")
    ended = re.compile(
        rf"canvas watch of {re.escape(path)} .*ended: the scope has no connected tab"
    )

    log = app_server.app.tail()
    try:
        await context.set_offline(True)
        await socket.wait_for_event("close", timeout=CanvasPage.TIMEOUT_MS)
        await log.until(ended, "the end of the watch without a connected tab")
    finally:
        async with panel.page.expect_websocket(timeout=CanvasPage.TIMEOUT_MS):
            await context.set_offline(False)

    await panel.rewrite("reconnect.png", PNG + bytes(1))
    await panel.until(
        PanelScript.IMAGE_REVISED,
        before,
        "the image link to get a new revision after the socket reconnected",
    )


# ——— Поведение: окна текста в панели (файл workspace, не журнал) ———


async def test_workspace_log_walks_by_windows(panel: CanvasPage) -> None:
    """Текстовый файл workspace листается окнами: «в конец» и «в начало»."""
    await panel.rewrite(
        "notes.log",
        "".join(f"N{index:06d},row\n" for index in range(120000)).encode(),
    )
    side = await panel.show("notes.log")

    head = await side.inner_text()

    await side.locator('button[data-canvas-action="to-end"]').first.click()
    await panel.await_text("N119999,row")
    tail = await side.inner_text()

    await side.locator('button[data-canvas-action="to-start"]').first.click()
    await panel.await_text("N000000,row")
    back = await side.inner_text()

    if "N000000,row" not in head:
        raise AssertionError("файл открылся не с начала")
    if "N119999,row" not in tail:
        raise AssertionError("«в конец» не показал хвост файла")
    if "N000000,row" in tail:
        raise AssertionError("хвост показан вместе с началом файла")
    if "N000000,row" not in back:
        raise AssertionError("«в начало» не вернуло к началу файла")


async def test_scrolling_up_loads_previous_window(panel: CanvasPage) -> None:
    """Прокрутка вверх подтягивает предыдущее окно и не дёргает позицию."""
    side = await panel.show("notes.log")
    page = panel.page

    await side.locator('button[data-canvas-action="to-end"]').first.click()
    await panel.await_text("N119999,row")

    before = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            return box.querySelector('pre').textContent.length;
        }"""
    )

    # прокрутка вверх повторяется, пока окно не подтянется; 0 → 0 браузер не
    # считает прокруткой, поэтому окно чередуется между «чуть ниже» и верхом
    try:
        await page.wait_for_function(
            """(before) => {
                const box = document.querySelector(
                  '#side-view-content [data-canvas-scroll]');
                if (box.querySelector('pre').textContent.length > before) {
                    return true;
                }
                box.scrollTop = box.scrollTop === 0 ? 40 : 0;
                return false;
            }""",
            arg=before,
            polling=500,
            timeout=CanvasPage.TIMEOUT_MS,
        )
    except PlaywrightTimeout as exc:
        raise AssertionError("прокрутка вверх не подтянула предыдущее окно") from exc


async def test_font_size_survives_expanding(panel: CanvasPage) -> None:
    """Размер шрифта переживает разворачивание: состояние сцены одно."""
    await panel.show("notes.log")
    page = panel.page

    read = "() => document.querySelector('[data-canvas-stage] pre').style.fontSize"

    await page.locator('button[data-canvas-action="larger"]').first.click()
    await page.locator('button[data-canvas-action="larger"]').first.click()
    before = await page.evaluate(read)

    await panel.expand()
    expanded = await page.evaluate(read)
    await panel.collapse()

    if before != "16px":
        raise AssertionError(f'before == "16px", получено {before}')
    if expanded != before:
        raise AssertionError("размер шрифта сбросился при разворачивании")


async def test_scroll_position_survives_expanding(panel: CanvasPage) -> None:
    """Прокрутка не прыгает при разворачивании: узел сцены тот же самый."""
    await panel.rewrite(
        "notes.log",
        "".join(f"S{index:06d},row\n" for index in range(20000)).encode(),
    )
    await panel.show("notes.log")
    page = panel.page

    scrollable = await page.evaluate(
        """() => {
            const box = document.querySelector(
              '#side-view-content [data-canvas-scroll]');
            box.scrollTop = 400;
            return box.scrollHeight > box.clientHeight;
        }"""
    )
    if scrollable is not True:
        raise AssertionError("содержимое короче окна: прокрутку не проверить")
    await page.evaluate(PanelScript.SETTLED)

    await panel.expand()
    kept = await page.evaluate(
        """() => document.querySelector(
            '[data-canvas-panel][data-full="true"] [data-canvas-scroll]').scrollTop"""
    )
    await panel.collapse()

    if abs(kept - 400) > 4:
        raise AssertionError(f"прокрутка съехала при разворачивании: {kept}")


# ——— Поведение: навигация и жизненный цикл панели ———


async def test_wheel_zooms_in_fullscreen(panel: CanvasPage) -> None:
    """На весь экран колесо зумит — в отличие от панели, где оно листает."""
    await panel.show("flow.mmd")
    page = panel.page
    scene = '[data-canvas-stage] div[style*="transform-origin"]'

    await panel.expand()
    before = await page.locator(scene).evaluate(PanelScript.TRANSFORM)

    viewport = page.viewport_size
    if viewport is None:
        raise AssertionError("the page has no viewport size")
    await page.mouse.move(viewport["width"] / 2, viewport["height"] / 2)
    await page.mouse.wheel(0, -400)
    await panel.transformed(scene, before)

    # в полном экране сцена живёт в body: кнопки ищем на странице, не в панели
    await page.locator('button[data-canvas-action="reset-view"]').first.click()
    await panel.collapse()


async def test_diagram_pans_by_dragging(panel: CanvasPage) -> None:
    """Диаграмму можно таскать мышью: сцена сдвигается вслед за курсором."""
    side = await panel.show("flow.mmd")
    page = panel.page
    scene = '#side-view-content div[style*="transform-origin"]'

    before = await page.locator(scene).evaluate(PanelScript.TRANSFORM)

    box = await side.bounding_box()
    if not box:
        raise AssertionError("панель без геометрии")

    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(start_x - 120, start_y - 60, steps=8)
    await page.mouse.up()
    await panel.transformed(scene, before)

    await side.locator('button[data-canvas-action="reset-view"]').first.click()


async def test_panel_reopens_after_closing(panel: CanvasPage) -> None:
    """Закрытую панель можно открыть снова тем же действием."""
    await panel.show("report.md")

    await panel.close()

    reopened = await panel.show("flow.mmd")
    if "Доход" not in await reopened.inner_text():
        raise AssertionError("панель не открылась повторно")


async def test_missing_file_is_explained(panel: CanvasPage) -> None:
    """Пропавший файл объясняется в панели, а не роняет её молча."""
    missing = {"path": panel.tool_view("нет-такого.md"), "mime": "text/markdown"}
    side = await panel.act("canvas_open", missing)

    text = await side.inner_text()
    if not text.strip():
        raise AssertionError("панель промолчала о пропавшем файле")


async def test_theme_change_redraws_the_diagram(panel: CanvasPage) -> None:
    """Смена темы перерисовывает диаграмму: mermaid рисует свою палитру, и
    разметка svg меняется туда и обратно."""
    await panel.show("flow.mmd")
    page = panel.page
    toggle = "() => document.documentElement.classList.toggle('dark')"

    before = await page.evaluate(PanelScript.SVG)
    await page.evaluate(toggle)
    await panel.until(PanelScript.SVG_CHANGED, before, "the diagram in the other theme")

    after = await page.evaluate(PanelScript.SVG)
    await page.evaluate(toggle)
    await panel.until(PanelScript.SVG_CHANGED, after, "the diagram in the first theme")


async def test_buttons_work_in_fullscreen(panel: CanvasPage) -> None:
    """Кнопки в полном экране действительно работают, а не просто нарисованы.

    Сцена уезжает из панели, и если поднять её мимо корня React, клики
    перестают доходить до обработчиков — снаружи это выглядит как мёртвые
    кнопки. Проверяем результат клика, а не его факт: клик — дискретное
    событие, и React применяет его состояние до того, как клик вернётся.
    """
    await panel.show("flow.mmd")
    page = panel.page
    full = '[data-canvas-panel][data-full="true"]'
    scene = page.locator(f'{full} div[style*="transform-origin"]')

    await panel.expand()
    before = await scene.evaluate(PanelScript.TRANSFORM)

    await page.locator(f'{full} button[data-canvas-action="zoom-in"]').click()
    zoomed = await scene.evaluate(PanelScript.TRANSFORM)

    await page.locator(f'{full} button[data-canvas-action="reset-view"]').click()
    reset = await scene.evaluate(PanelScript.TRANSFORM)

    if zoomed == before:
        raise AssertionError("кнопка зума не работает в полном экране")
    if reset != before:
        raise AssertionError("сброс вида не работает в полном экране")

    await page.locator(f'{full} button[data-canvas-action="close"]').click()
    await panel.until(
        PanelScript.COLLAPSED, None, "the close button to collapse the full screen"
    )


DOWNLOADABLE = (
    "chart.png",
    "report.md",
    "flow.mmd",
    "notes.log",
    "data.bin",
    "report.pdf",
)
"""Все типы показа: картинка, markdown, диаграмма, лог, pdf и формат без вьювера."""


async def test_every_viewer_offers_download(panel: CanvasPage) -> None:
    """Кнопка скачивания есть у любого показанного файла: панель всегда о файле.

    Ссылку проставляет база вьюверов, а рисует кнопку общая шапка сцены —
    поэтому наследники получают её без собственного кода. Шапка узнаёт файл
    показа после монтирования сцены, поэтому кнопку тест ждёт.
    """
    for name in DOWNLOADABLE:
        side = await panel.show(name)
        button = side.locator('button[data-canvas-action="download"]')

        await button.first.wait_for(timeout=CanvasPage.TIMEOUT_MS)
        found = await button.count()
        if found != 1:
            raise AssertionError(f"{name}: кнопки скачивания нет (найдено {found})")


async def test_download_saves_the_shown_file(panel: CanvasPage) -> None:
    """Кнопка отдаёт именно показанный файл под его собственным именем."""
    for name in ("chart.png", "flow.mmd", "report.md"):
        side = await panel.show(name)

        async with panel.page.expect_download() as pending:
            await side.locator('button[data-canvas-action="download"]').click()

        saved = await pending.value
        if saved.suggested_filename != name:
            raise AssertionError(f"{name}: скачался как {saved.suggested_filename}")


async def test_download_works_in_fullscreen(panel: CanvasPage) -> None:
    """В полном экране кнопка та же и так же работает."""
    await panel.show("report.md")
    page = panel.page

    await panel.expand()
    async with page.expect_download() as pending:
        await page.locator(
            '[data-canvas-stage] button[data-canvas-action="download"]'
        ).click()
    saved = await pending.value
    await panel.collapse()

    if saved.suggested_filename != "report.md":
        raise AssertionError(f"скачался {saved.suggested_filename}")


async def test_preview_card_has_no_download_button(panel: CanvasPage) -> None:
    """Карточка в ленте не панель: у неё своя ссылка на исходник, не кнопка."""
    await panel.show("flow.mmd")

    in_feed = await panel.page.evaluate(
        """() => document.querySelectorAll(
            '.message-content button[data-canvas-action="download"]').length"""
    )

    if in_feed:
        raise AssertionError("в карточке ленты не должно быть кнопки скачивания")


async def test_hostile_diagram_runs_no_script(panel: CanvasPage) -> None:
    """HTML в подписи и click в спеке mermaid не исполняются (7.6).

    Доказательство — атрибуты svg: без on* и javascript: скрипту не на чем
    сработать; ловушка window.__boba_pwned лишь подтверждает это.
    """
    side = await panel.show("hostile.mmd")
    page = panel.page

    svg = side.locator('div[style*="transform-origin"] svg')
    if await svg.count() <= 0:
        raise AssertionError("the hostile diagram is not drawn")

    # ловушка для click A call: strict обязан не привязать вызов к узлу
    await page.evaluate(
        "() => { window.__boba_click = () => { window.__boba_pwned = 'click'; }; }"
    )
    for node in await svg.first.locator("g.node").all():
        await node.click(force=True)
    await page.evaluate(PanelScript.SETTLED)

    handlers = await page.evaluate(
        """() => {
            const svg = document.querySelector(
                '#side-view-content div[style*="transform-origin"] svg');
            const found = [];
            for (const el of svg.querySelectorAll('*')) {
                for (const attr of el.attributes) {
                    const name = attr.name.toLowerCase();
                    const value = attr.value.toLowerCase();
                    if (name.startsWith('on') || value.startsWith('javascript:')) {
                        found.push(`${el.tagName}.${attr.name}`);
                    }
                }
            }
            return found;
        }"""
    )

    if handlers:
        raise AssertionError(f"script hooks left in the diagram svg: {handlers}")

    pwned = await page.evaluate("() => window.__boba_pwned")
    if pwned is not None:
        raise AssertionError(f"the diagram ran a script: {pwned!r}")


async def test_hostile_svg_runs_no_script(panel: CanvasPage) -> None:
    """SVG со <script> и onload показывается картинкой, скрипт не исполняется."""
    side = await panel.show("hostile.svg")
    page = panel.page

    loaded = await side.locator("img").evaluate(PanelScript.IMAGE_LOADED)
    if loaded is not True:
        raise AssertionError("the hostile svg is not shown as a loaded image")
    if await side.locator("svg script").count():
        raise AssertionError("the svg got into the page instead of an <img>")

    pwned = await page.evaluate("() => window.__boba_pwned")
    if pwned is not None:
        raise AssertionError(f"the svg ran a script: {pwned!r}")


async def test_canvas_route_downloads_what_the_browser_would_run(
    panel: CanvasPage,
) -> None:
    """Ссылка канваса открывает в браузере только видео и звук; html, svg и
    pdf скачиваются: первые не исполняются в origin чата (7.6), pdf рисует
    pdf.js на странице чата."""
    await panel.show("chart.png")
    page = panel.page
    cookie = await _cookie_header(page.context)

    expected = {
        "page.html": "attachment",
        "hostile.svg": "attachment",
        "data.bin": "attachment",
        "report.pdf": "attachment",
        "clip.wav": "inline",
    }
    for name, kind in expected.items():
        query = urlencode({"mime": ContentMime.of_name(name)})
        url = f"{BASE}/canvas/{panel.thread}/upload/{name}?{query}"
        response = await page.request.get(url, headers={"cookie": cookie})
        if response.status != 200:
            raise AssertionError(f"GET {url}: expected 200, got {response.status}")

        disposition = response.headers.get("content-disposition", "")
        if not disposition.startswith(f"{kind};"):
            raise AssertionError(f"{name}: expected {kind}, got {disposition!r}")

        sniffing = response.headers.get("x-content-type-options", "")
        if sniffing != "nosniff":
            raise AssertionError(f"{name}: expected nosniff, got {sniffing!r}")


async def test_uploaded_html_is_downloaded(panel: CanvasPage) -> None:
    """Вложение пользователя с html и pdf отдаётся скачиванием, тело — как
    загружено."""
    await panel.show("chart.png")
    page = panel.page
    session_id = panel.probe.session["id"]
    cookie = await _cookie_header(page.context)

    expected = {
        ("page.html", "text/html"): ("attachment", HOSTILE_HTML),
        ("report.pdf", "application/pdf"): ("attachment", PDF),
    }
    for (name, mime), (kind, blob) in expected.items():
        uploaded = await page.request.post(
            f"{BASE}/project/file?session_id={session_id}",
            multipart={"file": {"name": name, "mimeType": mime, "buffer": blob}},
            headers={"cookie": cookie},
        )
        if uploaded.status != 200:
            body = await uploaded.text()
            raise AssertionError(f"upload {name}: {uploaded.status} {body}")

        file_id = (await uploaded.json())["id"]
        url = f"{BASE}/project/file/{file_id}?session_id={session_id}"
        response = await page.request.get(url, headers={"cookie": cookie})
        if response.status != 200:
            raise AssertionError(f"GET {url}: expected 200, got {response.status}")

        downloaded = await response.body()
        if downloaded != blob:
            raise AssertionError(f"{name}: the download differs from the upload")

        disposition = response.headers.get("content-disposition", "")
        if not disposition.startswith(f"{kind};"):
            raise AssertionError(f"{name}: expected {kind}, got {disposition!r}")

        sniffing = response.headers.get("x-content-type-options", "")
        if sniffing != "nosniff":
            raise AssertionError(f"{name}: expected nosniff, got {sniffing!r}")


async def test_link_mime_wins_over_the_file_name(panel: CanvasPage) -> None:
    """Ссылка называет вид файла сама: pdf без суффикса рисует вьювер pdf,
    документ он запрашивает ссылкой с этим видом, а маршрут отдаёт его как
    pdf на скачивание."""
    await panel.show("chart.png")
    page = panel.page
    asked: list[str] = []

    def remember(request: Any) -> None:
        if "/upload/report?" in request.url:
            asked.append(request.url)

    page.on("request", remember)
    try:
        path = panel.tool_view("report")
        side = await panel.act("canvas_open", {"path": path, "mime": "application/pdf"})
        await panel.await_text("Alpha page one")
    finally:
        page.remove_listener("request", remember)

    if not asked:
        shown = await side.inner_html()
        raise AssertionError(f"pdf.js asked nothing for the file: {shown}")

    url = asked[0]
    if "mime=application%2Fpdf" not in url:
        raise AssertionError(f"the link names the type of the file: {url}")

    cookie = await _cookie_header(page.context)
    response = await page.request.get(url, headers={"cookie": cookie})
    if response.status != 200:
        raise AssertionError(f"GET {url}: expected 200, got {response.status}")

    disposition = response.headers.get("content-disposition", "")
    if response.headers.get("content-type") != "application/pdf":
        raise AssertionError(f"the route serves a pdf: {response.headers}")
    if not disposition.startswith("attachment;"):
        raise AssertionError(f"the pdf is downloaded, not shown: {disposition!r}")


# ——— PDF: pdf.js из сборки chainlit ———


async def test_pdf_is_drawn_by_pdfjs(panel: CanvasPage) -> None:
    """PDF рисует pdf.js на странице чата: без рамки встроенного вьювера,
    холст страницы и текстовый слой, счётчик страниц и панель кнопок видна
    без наведения; в узком окне панель не уходит вбок."""
    side = await panel.show("report.pdf")
    page = panel.page
    await panel.await_text("Alpha page one")

    if await side.locator("iframe").count():
        raise AssertionError("the pdf is drawn in a frame of the browser viewer")

    state = await panel.pdf_state()
    if state["width"] <= 0 or state["height"] <= 0:
        raise AssertionError(f"the page canvas has no size: {state}")
    if "Alpha page one" not in state["text"]:
        raise AssertionError(f"the text layer carries the page text: {state}")
    if "1 / 2" not in state["bar"] or "100%" not in state["bar"]:
        raise AssertionError(f"the toolbar shows the page and the zoom: {state}")
    if state["opacity"] != "1":
        raise AssertionError(f"the toolbar is shown without hovering: {state}")

    size = page.viewport_size
    if size is None:
        raise AssertionError("the page has no viewport size")
    await page.set_viewport_size({"width": 420, "height": 800})
    try:
        await panel.until(
            PanelScript.PDF_FITS,
            420,
            "the pdf drawn in a narrow window without a sideways scroll",
        )
    finally:
        await page.set_viewport_size(size)


async def test_pdf_turns_pages_and_zooms(panel: CanvasPage) -> None:
    """Листание и масштаб: «›» ведёт на вторую страницу и упирается в
    последнюю, «‹» заперта на первой; масштаб шагами по 25 % от 50 % до
    300 %, холст растёт вместе с ним."""
    side = await panel.show("report.pdf")
    await panel.await_text("Alpha page one")

    previous = side.locator("[data-canvas-stage] button:has(svg.lucide-chevron-left)")
    following = side.locator("[data-canvas-stage] button:has(svg.lucide-chevron-right)")
    if not await previous.is_disabled():
        raise AssertionError("the first page cannot go back")

    await panel.pdf_click("chevron-right")
    await panel.await_text("Beta page two")
    turned = await panel.pdf_state()
    if "2 / 2" not in turned["bar"]:
        raise AssertionError(f"the counter follows the page: {turned}")
    if not await following.is_disabled():
        raise AssertionError("the last page cannot go further")

    await panel.pdf_click("chevron-left")
    await panel.await_text("Alpha page one")

    before = await panel.pdf_state()
    await panel.pdf_click("zoom-in")
    zoomed = await panel.pdf_state()
    if "125%" not in zoomed["bar"]:
        raise AssertionError(f"one step up is 125%: {zoomed}")
    # холст pdf.js перерисовывает после смены масштаба сам: ждём его рост
    await panel.until(
        PanelScript.PDF_WIDER, before["width"] * 1.1, "the canvas to grow with the zoom"
    )

    zoom_in = side.locator("[data-canvas-stage] button:has(svg.lucide-zoom-in)")
    for _ in range(10):
        if await zoom_in.is_disabled():
            break
        await panel.pdf_click("zoom-in")
    if "300%" not in (await panel.pdf_state())["bar"]:
        raise AssertionError("zooming in stops at 300%")

    zoom_out = side.locator("[data-canvas-stage] button:has(svg.lucide-zoom-out)")
    for _ in range(12):
        if await zoom_out.is_disabled():
            break
        await panel.pdf_click("zoom-out")
    smallest = (await panel.pdf_state())["bar"]
    if not re.search(r"(^|\s)50%", smallest):
        raise AssertionError(f"the zoom stops at 50%: {smallest!r}")


async def test_pdf_reloads_when_the_file_changes(panel: CanvasPage) -> None:
    """Перезаписанный pdf перечитывается в той же панели по сигналу слежения."""
    await panel.rewrite("revised.pdf", PDF)
    side = await panel.show("revised.pdf")
    await panel.await_text("Alpha page one")

    stage = await side.locator("[data-canvas-stage]").element_handle()
    await panel.rewrite("revised.pdf", PDF_REVISED)
    await panel.await_text("Gamma revised")

    same = await panel.page.evaluate("(stage) => stage.isConnected", stage)
    if same is not True:
        raise AssertionError("the panel is rebuilt instead of reloading the document")


async def test_pdf_worker_loads_under_the_prefix(panel: CanvasPage) -> None:
    """Воркер pdf.js берётся из сборки под префиксом чата: запасной
    «fake worker» в главном потоке не поднимается."""
    await panel.show("chart.png")
    page = panel.page
    workers: list[str] = []
    console: list[str] = []

    def on_worker(worker: Any) -> None:
        workers.append(worker.url)

    def on_console(message: Any) -> None:
        console.append(message.text)

    page.on("worker", on_worker)
    page.on("console", on_console)
    try:
        await panel.show("report.pdf")
        await panel.await_text("Alpha page one")
    finally:
        page.remove_listener("worker", on_worker)
        page.remove_listener("console", on_console)

    loaded: list[str] = []
    for url in workers:
        if url.startswith(f"{BASE}/assets/pdf.worker"):
            loaded.append(url)

    if not loaded:
        raise AssertionError(f"no pdf.js worker under {BASE}/assets: {workers}")

    response = await page.request.get(loaded[0])
    if response.status != 200:
        raise AssertionError(f"GET {loaded[0]}: expected 200, got {response.status}")

    faked: list[str] = []
    for text in console:
        if "fake worker" in text.lower():
            faked.append(text)

    if faked:
        raise AssertionError(f"pdf.js fell back to a fake worker: {faked}")


class TabEvents:
    """Что делает вкладка чата под чужим документом: диалоги, загрузка
    песочницы скриптов pdf.js и навигация главного фрейма.

    Навигация ловится запросом, который уходит в момент клика, — ждать,
    пока сменится адрес вкладки, не нужно. Слушает страницу между attach и
    detach; создаётся тестом враждебного pdf."""

    SANDBOX: ClassVar[str] = "pdf.sandbox"

    def __init__(self, page: Page) -> None:
        self._page = page
        self.dialogs: list[str] = []
        self.sandboxes: list[str] = []
        self.navigations: list[str] = []

    def attach(self) -> None:
        self._page.on("dialog", self._on_dialog)
        self._page.on("request", self._on_request)

    def detach(self) -> None:
        self._page.remove_listener("dialog", self._on_dialog)
        self._page.remove_listener("request", self._on_request)

    def _on_dialog(self, dialog: Any) -> None:
        self.dialogs.append(dialog.message)

    def _on_request(self, request: Any) -> None:
        if self.SANDBOX in request.url:
            self.sandboxes.append(request.url)

        if not request.is_navigation_request():
            return

        if request.frame == self._page.main_frame:
            self.navigations.append(request.url)


async def test_hostile_pdf_runs_no_script(panel: CanvasPage) -> None:
    """JavaScript документа не исполняется: ни при открытии, ни по ссылке,
    ни у поля с вычислением; ссылка javascript: не становится адресом,
    диалогов нет, вкладка чата остаётся на месте, а внешняя ссылка
    открывается новой вкладкой."""
    await panel.show("chart.png")
    page = panel.page
    events = TabEvents(page)

    events.attach()
    try:
        side = await panel.show("hostile.pdf")
        await panel.await_text("Hostile page")
        address = page.url

        links = side.locator(".annotationLayer a")
        hrefs: list[str] = []
        for index in range(await links.count()):
            hrefs.append(str(await links.nth(index).get_attribute("href")))

        for index in range(await links.count()):
            link = links.nth(index)
            href = str(await link.get_attribute("href"))
            if href.startswith("https://"):
                continue
            await link.click(force=True)

        external = side.locator('.annotationLayer a[href^="https://example.invalid"]')
        async with page.context.expect_page() as opened:
            await external.click(force=True)
        popup = await opened.value
        await popup.close()
        await page.evaluate(PanelScript.SETTLED)
    finally:
        events.detach()

    scripted: list[str] = []
    for href in hrefs:
        if href.lower().startswith("javascript:"):
            scripted.append(href)

    if scripted:
        raise AssertionError(f"javascript: links reached the page: {scripted}")
    if events.dialogs:
        raise AssertionError(f"the document opened dialogs: {events.dialogs}")
    if events.sandboxes:
        raise AssertionError(f"the pdf.js sandbox was loaded: {events.sandboxes}")
    if events.navigations:
        raise AssertionError(f"a link navigated the chat tab: {events.navigations}")
    if page.url != address:
        raise AssertionError(f"a link led the chat away: {address} -> {page.url}")

    pwned = await page.evaluate("() => window.__boba_pwned")
    if pwned is not None:
        raise AssertionError(f"the pdf ran a script: {pwned!r}")


async def test_panel_follows_the_file_after_a_storage_outage(
    panel: CanvasPage, app_server: E2eProcesses
) -> None:
    """Сервис файлов недоступен (перезапуск): опрос версии файла отказывает,
    но слежение не кончается — отказ виден в журнале чата, опрос
    повторяется, и правка файла после возврата сервиса доходит до панели.

    Тест последний в модуле: он перезапускает сервис стенда."""
    side = await panel.show("outage.png")
    before = await side.locator("img").evaluate("img => img.src")
    failed = re.compile(r"stat of \S*/outage\.png failed")

    log = app_server.app.tail()
    await asyncio.to_thread(app_server.service.stop)
    try:
        await log.until(failed, "a failed stat of outage.png while the service is down")
    finally:
        await asyncio.to_thread(app_server.service.start)

    await panel.rewrite("outage.png", PNG + bytes(1))
    await panel.until(
        PanelScript.IMAGE_REVISED,
        before,
        "the image link to get a new revision after the storage came back",
    )
