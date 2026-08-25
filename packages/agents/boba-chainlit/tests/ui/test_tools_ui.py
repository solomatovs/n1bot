"""Каждый инструмент приложения вызывается ходом и рисуется в ленте шагом.

Стенд поднимается с песочницей — тем же путём, что и прод: зигота секции,
исполнитель вызова, тело инструмента. Фейковая модель делает tool_call,
продиктованный тестом. После хода сверяются последний шаг инструмента —
имя, вход и результат по кадрам socket.io, которыми фронт рисует ленту, —
и разметка раскрытого шага в DOM. Ожидания точные; там, где данные живые
(Confluence, размеры каталога), — регулярные выражения.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from boba.chainlit.canvas.diagram import DiagramPrompt
from boba.chainlit.rendering.tool import ToolCallMarkdown, ToolResultMarkdown
from boba.liteparse.engine import LiteParseEngine
from boba.settings import bind, build_app_config
from boba.text.document import LiteParseParams
from boba.text.grep import GrepLimits, TextGrep
from boba.tool.kb.confluence.parsing import ConfluenceJson
from boba.tool.kb.confluence.request_sources import ConfluenceRest
from boba.tool.kb.confluence.tools import ConfluenceToolsConfig, CqlSearch
from boba.tool.kb.html.payload import PageOps
from boba.toolkit.calls import JsonCall, ScriptCall
from boba.toolkit.result import (
    AffectedSqlResult,
    ErrorResult,
    MultiResult,
    TableResult,
    TextResult,
    ToolResult,
)
from ui.chat_page import ChatPage, StepKind
from ui.conftest import ChatOpener, StandDatabase
from ui.fake_llm import FakePage, FakeRoute, ScenarioName
from ui.socket_log import StepField
from ui.stand import (
    REPO_ROOT,
    StandConfig,
    StandPaths,
    StandProcess,
    StandUrl,
    free_port,
)

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 180.0
"""Подъём стенда с песочницей: восемь зигот, у kb — прогрев эмбеддера."""

TURN_TIMEOUT_SEC = 180.0

INGEST_TIMEOUT_SEC = 600.0
"""Индексация страницы: эмбеддер e5-large в песочнице считает на CPU."""

STREAM_ELEMENT = "CanvasStream"
"""Имя элемента кнопки живого вывода на шаге инструмента песочницы."""

CANVAS_ELEMENT = "CanvasView"
"""Имя элемента карточки диаграммы в ленте."""


class StepMark(StrEnum):
    """Статусный кружок в названии шага: им лента показывает исход вызова."""

    DONE = "✔"
    FAILED = "✖"

    @classmethod
    def of(cls, ok: bool) -> StepMark:
        if ok:
            return cls.DONE

        return cls.FAILED


class ProbeFile(StrEnum):
    """Файлы стенда в образе пользователя: их кладёт bash, читают doc-тулы."""

    DIR = "/workspace/ui-probe"
    PDF = "/workspace/ui-probe/sample.pdf"


class ProbeText(StrEnum):
    """Маркеры, по которым тест узнаёт свой след в ленте."""

    BASH_ECHO = "ui-probe-bash"
    BASH_STDERR = "ui-probe-stderr"
    PDF_PAGE_ONE = "Alpha page one"
    PDF_PAGE_TWO = "Beta page two Alpha again"
    PDF_QUERY = "Alpha"
    NOTHING = "zzzz-ui-nothing"
    MISSING_THREAD = "no-such-thread"
    OUTSIDE_PATH = "/workspace/elsewhere/outside.png"
    NO_SPACE = "NOSUCHSPACE"
    CONFLUENCE_QUERY = "данные"
    ATTACHMENT_QUERY = "docx"


class ProbeDiagram(StrEnum):
    """Диаграмма стенда: имя файла и спека."""

    NAME = "orders.mmd"
    SPEC = "erDiagram\n    USER ||--o{ ORDER : places"


class ProbeSql(StrEnum):
    """Таблица стенда в базе соединения main и запросы к ней."""

    TABLE = "ui_probe"
    SCHEMA = "public"
    CREATE = (
        "drop table if exists public.ui_probe; "
        "create table public.ui_probe "
        "(id integer primary key, name text not null, note text); "
        "insert into public.ui_probe (id, name) values (1, 'alpha'), (2, 'beta')"
    )
    UPDATE = "update public.ui_probe set note = 'seen' where id = 1"
    SELECT = "select id, name from public.ui_probe order by id"
    COPY = (
        "COPY (select id, name from public.ui_probe order by id) "
        "TO STDOUT WITH (FORMAT CSV, HEADER)"
    )
    COPY_TEXT = "id,name\n1,alpha\n2,beta\n"
    CH_SELECT = "select currentUser() as who, 1 as a"
    CH_USER = "boba-svc"
    CH_SYSTEM = "system"
    CH_ONE = "one"


class RowWindowArgs:
    """Окно выдачи каталожных инструментов: одно на все вызовы стенда."""

    OFFSET: ClassVar[int] = 0
    MAX_ROWS: ClassVar[int] = 10
    MAX_CHARS: ClassVar[int] = 10000

    @classmethod
    def of(cls, max_rows: int = MAX_ROWS) -> dict[str, Any]:
        return {"offset": cls.OFFSET, "max_rows": max_rows, "max_chars": cls.MAX_CHARS}


class OcrArgs:
    """Параметры парсера документов: OCR выключен, как у текстовых pdf."""

    TESSDATA: ClassVar[str] = "/usr/share/tessdata"
    """Как в [tool.doc]/[tool.ingest]: параметр обязателен, OCR не включается."""

    @staticmethod
    def of() -> dict[str, Any]:
        return {"ocr_enabled": False, "num_workers": 1, "ocr_language": "rus+eng"}

    @classmethod
    def liteparse(cls) -> LiteParseParams:
        params = dict(cls.of())
        params["tessdata_path"] = cls.TESSDATA
        return LiteParseParams.model_validate(params)


class SamplePdf:
    """Двухстраничный PDF с xref: стр.1 и стр.2 — тексты ProbeText."""

    PAGES: ClassVar[tuple[str, ...]] = (
        ProbeText.PDF_PAGE_ONE.value,
        ProbeText.PDF_PAGE_TWO.value,
    )

    @classmethod
    def content(cls) -> bytes:
        objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>"]

        kids: list[str] = []
        for index in range(len(cls.PAGES)):
            kids.append(f"{3 + 2 * index} 0 R")
        objects.append(
            f"<</Type/Pages/Kids[{' '.join(kids)}]/Count {len(cls.PAGES)}>>".encode()
        )

        font = 3 + 2 * len(cls.PAGES)
        for index, text in enumerate(cls.PAGES):
            contents = 4 + 2 * index
            objects.append(
                (
                    f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]"
                    f"/Contents {contents} 0 R/Resources<</Font<</F1 {font} 0 R>>>>>>"
                ).encode()
            )
            stream = f"BT /F1 20 Tf 20 200 Td ({text}) Tj ET".encode()
            objects.append(
                b"<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream"
            )

        objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

        return cls._assemble(objects)

    @staticmethod
    def _assemble(objects: Sequence[bytes]) -> bytes:
        out = bytearray(b"%PDF-1.4\n")
        offsets: list[int] = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()

        out += (
            f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
        return bytes(out)

    @classmethod
    def base64(cls) -> str:
        return base64.b64encode(cls.content()).decode("ascii")


@dataclass(frozen=True)
class ToolCall:
    """Вызов, который фейковая модель сделает за тест: инструмент и аргументы."""

    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    view: JsonCall | ScriptCall = field(default_factory=JsonCall)
    """Как лента рисует вход шага: json либо скрипт с языком."""

    @property
    def intent(self) -> str:
        """Подпись вызова, которую фейк дописывает за отсутствием своей."""
        return f"stand call of {self.tool}"

    def message(self) -> str:
        request = {"name": self.tool, "arguments": dict(self.arguments)}
        return f"{ScenarioName.CALL.value} {json.dumps(request, ensure_ascii=False)}"

    def expected_input(self) -> str | None:
        """Вход шага, каким его рисует лента из аргументов без подписи."""
        if not self.arguments:
            return None

        rendering = ToolCallMarkdown(self.view, self.arguments).render()
        if rendering is None:
            return None

        return rendering.markdown


@dataclass(frozen=True)
class ToolExpect:
    """Что обязано быть в шаге после хода."""

    mark: StepMark = StepMark.DONE
    output: str | None = None
    """Точный markdown результата; None — сверка по patterns."""
    patterns: Sequence[str] = ()
    """Регулярные выражения (MULTILINE), каждое обязано найтись в результате."""
    dom: Sequence[str] = ()
    """Фрагменты текста, которые обязаны быть в раскрытом шаге DOM."""
    log_errors: bool = False
    """Ход вправе оставить ошибки в логе стенда: отказ ожидаем."""

    @classmethod
    def of(cls, result: ToolResult, dom: Sequence[str] = ()) -> ToolExpect:
        """Ожидание из модели результата: лента рисует её тем же рендером."""
        return cls(
            mark=StepMark.of(result.ok),
            output=ToolResultMarkdown(result).render(),
            dom=dom,
        )


@dataclass(frozen=True)
class ToolStep:
    """Шаг инструмента, каким его получила вкладка."""

    payload: Mapping[str, Any]
    dom_text: str

    @property
    def name(self) -> str:
        return str(self.payload.get(StepField.NAME.value) or "")

    @property
    def input(self) -> str:
        return str(self.payload.get(StepField.INPUT.value) or "")

    @property
    def output(self) -> str:
        return str(self.payload.get(StepField.OUTPUT.value) or "")

    @property
    def thread_id(self) -> str:
        return str(self.payload.get(StepField.THREAD_ID.value) or "")


class StepCheck:
    """Сверка шага с ожиданием: имя, вход, результат, DOM."""

    ELAPSED: ClassVar[str] = r"(?: · (?:\d+ ms|\d+\.\d s|\d+ m \d+ s))?"
    """Хвост имени с длительностью вызова: у сорвавшегося шага его нет."""

    def __init__(self, step: ToolStep, call: ToolCall, expect: ToolExpect) -> None:
        self._step = step
        self._call = call
        self._expect = expect

    def run(self) -> None:
        self._check_name()
        self._check_input()
        self._check_output()
        self._check_dom()

    def _check_name(self) -> None:
        label = f"{self._expect.mark.value} {self._call.tool} · {self._call.intent}"
        pattern = f"^{re.escape(label)}{self.ELAPSED}$"
        if re.fullmatch(pattern, self._step.name):
            return

        self._fail(f"step name {self._step.name!r} is not {label!r}")

    def _check_input(self) -> None:
        expected = self._call.expected_input()
        if expected is None:
            return

        if self._step.input == expected:
            return

        self._fail(
            f"step input differs\nexpected:\n{expected}\ngot:\n{self._step.input}"
        )

    def _check_output(self) -> None:
        expected = self._expect.output
        if expected is not None and self._step.output != expected:
            self._fail(
                f"step output differs\nexpected:\n{expected}\ngot:\n{self._step.output}"
            )

        for pattern in self._expect.patterns:
            if re.search(pattern, self._step.output, re.MULTILINE):
                continue

            self._fail(f"pattern {pattern!r} not in output:\n{self._step.output}")

    def _check_dom(self) -> None:
        title = f"{self._expect.mark.value} {self._call.tool} · {self._call.intent}"
        fragments = [title, *self._expect.dom]
        for fragment in fragments:
            if fragment in self._step.dom_text:
                continue

            self._fail(f"{fragment!r} is not in the DOM step:\n{self._step.dom_text}")

    def _fail(self, message: str) -> None:
        raise AssertionError(f"tool {self._call.tool}: {message}")


class Coverage:
    """Инструменты, которые прогон вызвал: сверяются со списком стенда."""

    called: ClassVar[set[str]] = set()


@dataclass
class ToolFeed:
    """Вкладка чата, через которую тест вызывает инструменты и читает ленту."""

    chat: ChatPage
    stand: StandProcess

    def call(
        self,
        call: ToolCall,
        expect: ToolExpect,
        timeout_sec: float = TURN_TIMEOUT_SEC,
    ) -> ToolStep:
        """Ход с вызовом инструмента; шаг сверяется с ожиданием и отдаётся."""
        Coverage.called.add(call.tool)
        log_mark = self.stand.log_lines()

        self.chat.ask(call.message())
        self.chat.await_idle(timeout_sec=timeout_sec)

        payload = self.chat.log.last_step(StepKind.TOOL.value)
        if payload is None:
            raise AssertionError(
                f"tool {call.tool}: no tool step in the socket log\n"
                f"{self.chat.log.describe()}\n{self.stand.tail(60)}"
            )

        node = self.chat.expand_last_tool()
        step = ToolStep(payload=payload, dom_text=node.inner_text())

        StepCheck(step, call, expect).run()

        if expect.log_errors:
            return step

        complaints = self.stand.complaints(since_line=log_mark)
        if complaints:
            raise AssertionError(
                f"tool {call.tool} left errors in the stand log:\n"
                + "\n".join(complaints[:10])
            )

        return step

    def thread_id(self) -> str:
        return self.chat.log.thread_id()


@dataclass(frozen=True)
class ConfluencePage:
    """Страница живого Confluence, найденная поиском; ожидания считаются из неё."""

    page_id: str
    title: str
    space_key: str
    space_name: str
    space_type: str
    html: str

    WORD: ClassVar[str] = r"[^\W\d_]{6,}"
    """Слово для grep и поиска: только буквы, чтобы regex и tsquery не спорили."""

    @property
    def markdown(self) -> str:
        answer = PageOps.to_markdown({"html": self.html, "heading_style": "ATX"})
        return str(answer["markdown"])

    @property
    def indexed_text(self) -> str:
        """Текст секций страницы: ровно то, что ingest кладёт в базу знаний."""
        answer = PageOps.confluence_sections({"html": self.html, "title": self.title})
        parts: list[str] = []
        for section in answer["sections"]:
            parts.append(str(section["content"]))

        return "\n".join(parts)

    @property
    def word(self) -> str:
        """Первое длинное слово секций, которое есть и в markdown.

        Текст до первого заголовка в индекс не попадает, а markdown несёт ещё
        и адреса ссылок: слово обязано быть в обоих.
        """
        markdown = self.markdown
        for found in re.finditer(self.WORD, self.indexed_text):
            word = found.group(0)
            if word in markdown:
                return word

        raise AssertionError(f"page {self.page_id} has no word to grep")


@dataclass(frozen=True)
class ConfluenceAttachment:
    """Вложение живого Confluence и его текст, разобранный тем же liteparse.

    Раскладка текста (отступы, переносы) зависит от шрифтов машины, поэтому
    сверяются слова, а не текст целиком.
    """

    page_id: str
    filename: str
    text: str

    WORDS: ClassVar[int] = 5

    @property
    def words(self) -> tuple[str, ...]:
        """Первые длинные слова текста без повторов."""
        found: list[str] = []
        for match in re.finditer(ConfluencePage.WORD, self.text):
            word = match.group(0)
            if word in found:
                continue

            found.append(word)
            if len(found) == self.WORDS:
                break

        if not found:
            raise AssertionError(f"attachment {self.filename} has no readable word")

        return tuple(found)


class ConfluenceSite:
    """Живой Confluence глазами теста: тот же профиль, что у инструментов."""

    SEARCH_LIMIT: ClassVar[int] = 10
    ATTACHMENT_LIMIT: ClassVar[int] = 20
    MIN_HTML_CHARS: ClassVar[int] = 200
    EXPAND: ClassVar[str] = "body.view,version,space"
    PAGE_ID_IN_URL: ClassVar[str] = r"pageId=(\d+)"

    def __init__(self, config: ConfluenceToolsConfig) -> None:
        self._config = config
        profile = config.confluence
        self._base = str(profile.base_url or "").rstrip("/")
        self._client = httpx.Client(
            timeout=profile.timeout_sec,
            verify=profile.ssl_verify,
            follow_redirects=True,
            auth=profile.httpx_auth(),
        )

    @classmethod
    def load(cls) -> ConfluenceSite:
        built = build_app_config(config_path=StandPaths.BASE_CONFIG.under(REPO_ROOT))
        config = bind(
            built, path=ConfluenceToolsConfig.SECTION, model=ConfluenceToolsConfig
        )
        return cls(config)

    @property
    def max_text_chars(self) -> int:
        return self._config.max_text_chars

    @property
    def base_url(self) -> str:
        return self._base

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str) -> dict[str, Any]:
        response = self._client.get(self._base + path)
        response.raise_for_status()
        return response.json()

    def get_bytes(self, path: str) -> bytes:
        response = self._client.get(self._base + path)
        response.raise_for_status()
        return response.content

    def find_page(self, query: str) -> ConfluencePage:
        """Самая короткая непустая страница из выдачи того же CQL, что у тула."""
        cql = CqlSearch.build_cql(query=query, spaces=None)
        path = ConfluenceRest.cql_search_path(
            cql, limit=self.SEARCH_LIMIT, start=0, expand=self.EXPAND
        )
        data = self.get_json(path)

        candidates = list(self._pages_of(data))
        if not candidates:
            pytest.skip(f"Confluence search {query!r} returned no global pages")

        candidates.sort(key=lambda page: len(page.html))
        return candidates[0]

    def _pages_of(self, data: Mapping[str, Any]) -> Iterator[ConfluencePage]:
        for hit in data.get("results") or []:
            title = str(hit.get("title") or "")
            if "." in title:
                continue

            space = hit.get("space")
            if not isinstance(space, dict):
                continue

            if str(space.get("type") or "") != "global":
                continue

            html = ConfluenceJson.body_html(hit, "view")
            if len(html) < self.MIN_HTML_CHARS:
                continue

            yield ConfluencePage(
                page_id=str(hit.get("id") or ""),
                title=title,
                space_key=str(space.get("key") or ""),
                space_name=str(space.get("name") or ""),
                space_type=str(space.get("type") or ""),
                html=html,
            )

    def find_attachment(self, query: str) -> ConfluenceAttachment:
        """Вложение .docx из поиска; текст считается тем же liteparse."""
        cql = CqlSearch.build_cql(query=query, spaces=None)
        path = ConfluenceRest.cql_search_path(cql, limit=self.ATTACHMENT_LIMIT, start=0)
        data = self.get_json(path)

        for hit in data.get("results") or []:
            title = str(hit.get("title") or "")
            if not title.endswith(".docx"):
                continue

            webui = str(hit.get("_links", {}).get("webui") or "")
            found = re.search(self.PAGE_ID_IN_URL, webui)
            if found is None:
                continue

            page_id = found.group(1)
            link = self._attachment_link(page_id, title)
            if not link:
                continue

            content = self.get_bytes(link)
            parsed = LiteParseEngine.parse_bytes(OcrArgs.liteparse(), content, title)
            return ConfluenceAttachment(
                page_id=page_id, filename=title, text=parsed.text
            )

        pytest.skip("Confluence search returned no .docx attachment")

    def _attachment_link(self, page_id: str, filename: str) -> str:
        path = ConfluenceRest.page_fetch_path(
            page_id, body_format=self._config.body_format
        )
        data = self.get_json(path)

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
            if not isinstance(links, dict):
                return ""

            return str(links.get("download") or "")

        return ""


@dataclass(frozen=True)
class GrepCase:
    """Ожидаемый отчёт grep'а по тексту: тот же TextGrep, что и в теле."""

    text: str
    pattern: str
    source: str
    clip_chars: int

    CONTEXT: ClassVar[int] = 0
    LIMIT: ClassVar[int] = 100

    def arguments(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "case_insensitive": False,
            "context": self.CONTEXT,
            "limit": self.LIMIT,
            "fixed_string": True,
        }

    def result(self) -> TextResult:
        compiled = TextGrep.compile_pattern(
            self.pattern, fixed_string=True, case_insensitive=False
        )
        limits = GrepLimits(
            context=self.CONTEXT, limit=self.LIMIT, clip_chars=self.clip_chars
        )
        report = TextGrep.report(self.text, compiled, limits, self.source)
        return TextResult(text=report.render(), language=report.LANG, note=report.note)


@dataclass(frozen=True)
class DiagramProbe:
    """Сохранённая диаграмма стенда: тред и путь файла в workspace."""

    thread_id: str

    @property
    def path(self) -> str:
        return f"/workspace/{self.thread_id}/mermaid/{ProbeDiagram.NAME.value}"


@pytest.fixture(scope="module")
def sandbox_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    """Стенд с песочницей: инструменты идут через зиготы, как в проде."""
    config = StandConfig(
        workdir=stand_workdir / "sandbox",
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-sandbox",
        sandbox=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "sandbox-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        # роли стенда в таблице появляются на старте: гранты кладутся после него
        StandDatabase(stand_database).seed_connections(llm_port)
        yield process
    finally:
        process.stop()


@pytest.fixture
def feed(sandbox_stand: StandProcess, open_chat: Any) -> ToolFeed:
    """Свежая вкладка на тест."""
    return ToolFeed(chat=open_chat(sandbox_stand), stand=sandbox_stand)


@pytest.fixture(scope="module")
def module_feed(sandbox_stand: StandProcess, module_chats: ChatOpener) -> ToolFeed:
    """Вкладка на модуль: подготовки, чей след нужен другим тестам."""
    return ToolFeed(chat=module_chats.open(sandbox_stand), stand=sandbox_stand)


@pytest.fixture(scope="module")
def probe_pdf(module_feed: ToolFeed) -> str:
    """PDF кладётся в образ пользователя bash'ем — как его положил бы сам
    пользователь; отдаётся тред, в чьём журнале остался этот вызов."""
    call = ToolCall(
        tool="bash",
        arguments={
            "command": (
                f"mkdir -p {ProbeFile.DIR.value} && base64 -d > {ProbeFile.PDF.value} "
                f"&& test -s {ProbeFile.PDF.value}"
            ),
            "stdin": SamplePdf.base64(),
        },
        view=ScriptCall(arg="command", lang="bash"),
    )
    expect = ToolExpect(
        output="_(no output)_\n\n_exit code: 0_",
        dom=["exit code: 0"],
    )
    step = module_feed.call(call, expect)
    return step.thread_id


@pytest.fixture(scope="module")
def confluence_site() -> Iterator[ConfluenceSite]:
    site = ConfluenceSite.load()
    try:
        yield site
    finally:
        site.close()


@pytest.fixture(scope="module")
def confluence_page(confluence_site: ConfluenceSite) -> ConfluencePage:
    return confluence_site.find_page(ProbeText.CONFLUENCE_QUERY.value)


@pytest.fixture(scope="module")
def confluence_attachment(confluence_site: ConfluenceSite) -> ConfluenceAttachment:
    return confluence_site.find_attachment(ProbeText.ATTACHMENT_QUERY.value)


@pytest.fixture(scope="module")
def indexed_page(
    module_feed: ToolFeed, confluence_page: ConfluencePage
) -> ConfluencePage:
    """Страница проиндексирована в базу знаний стенда: поиск ищет по ней."""
    call = ToolCall(
        tool="confluence_index_pages",
        arguments={
            "page_ids": [confluence_page.page_id],
            "prune_missing": False,
            "force_update": True,
        },
    )
    expect = ToolExpect(
        patterns=[
            TablePattern.row(
                "collection", "indexed", "skipped_unchanged", "pruned", "failed"
            ),
            TablePattern.row("kb_confluence", r"[1-9]\d*", "0", r"\d+", "0"),
            f"^_page_ids \\(1\\): {confluence_page.page_id}_$",
        ],
        dom=["kb_confluence", f"page_ids (1): {confluence_page.page_id}"],
    )
    module_feed.call(call, expect, timeout_sec=INGEST_TIMEOUT_SEC)
    return confluence_page


@pytest.fixture(scope="module")
def probe_table(module_feed: ToolFeed) -> str:
    """Таблица стенда создаётся pg_query: набор команд одной транзакцией."""
    call = ToolCall(
        tool="pg_query",
        arguments={"connection_name": "main", "sql": ProbeSql.CREATE.value},
        view=ScriptCall(arg="sql", lang="sql"),
    )
    result = MultiResult(
        items=[
            AffectedSqlResult(affected_rows=None, status="DROP TABLE"),
            AffectedSqlResult(affected_rows=None, status="CREATE TABLE"),
            AffectedSqlResult(affected_rows=2, status="INSERT 0 2"),
        ]
    )
    expect = ToolExpect.of(result, dom=["DROP TABLE", "CREATE TABLE", "INSERT 0 2"])
    module_feed.call(call, expect)
    return ProbeSql.TABLE.value


@pytest.fixture(scope="module")
def canvas_feed(sandbox_stand: StandProcess, module_chats: ChatOpener) -> ToolFeed:
    """Вкладка для тулов ленты: файлы треда видны только из его же чата."""
    return ToolFeed(chat=module_chats.open(sandbox_stand), stand=sandbox_stand)


@pytest.fixture(scope="module")
def saved_diagram(canvas_feed: ToolFeed) -> DiagramProbe:
    """Диаграмма сохранена diagram_save; путь файла назван в ответе тула."""
    usage = ToolCall(tool="stream_logs_usage")
    step = canvas_feed.call(usage, ToolExpect(patterns=[UsagePattern.VOLUME]))
    probe = DiagramProbe(thread_id=step.thread_id)

    call = ToolCall(
        tool="diagram_save",
        arguments={"name": ProbeDiagram.NAME.value, "spec": ProbeDiagram.SPEC.value},
        view=ScriptCall(arg="spec", lang="mermaid"),
    )
    result = TextResult(
        text=f"diagram saved: {probe.path}; {DiagramPrompt.SAVED_NOTE.value}"
    )
    canvas_feed.call(call, ToolExpect.of(result, dom=[f"diagram saved: {probe.path}"]))
    return probe


class TablePattern:
    """Регулярные выражения по строкам github-таблицы ленты."""

    @staticmethod
    def row(*cells: str) -> str:
        """Целая строка таблицы: ячейки — regex-фрагменты по порядку колонок."""
        parts: list[str] = []
        for cell in cells:
            parts.append(f" {cell} +")

        return "^\\|" + "\\|".join(parts) + "\\|$"

    @staticmethod
    def cells(*cells: str) -> str:
        """Соседние ячейки где-то в строке: колонки вокруг не важны."""
        parts: list[str] = []
        for cell in cells:
            parts.append(f" {cell} +")

        return "\\|" + "\\|".join(parts) + "\\|"


class UsagePattern:
    """Строки отчёта stream_logs_usage: объёмы живые, форма фиксирована."""

    SIZE: ClassVar[str] = r"\d+(?:\.\d+)? (?:KiB|MiB|GiB)"
    VOLUME: ClassVar[str] = f"^volume: {SIZE} used of {SIZE}, {SIZE} free$"
    THREADS: ClassVar[str] = r"^journals by thread, oldest first:$"

    @classmethod
    def thread(cls, thread_id: str) -> str:
        return f"^- {re.escape(thread_id)}: {cls.SIZE} in \\d+ calls$"


def _connections(name: str) -> TableResult:
    return TableResult(rows=[{"connection_name": name}])


def _web_connections() -> TableResult:
    """Web-список несёт ещё и хост профиля: у стенда это адрес фейка."""
    return TableResult(rows=[{"connection_name": "stand", "host": StandUrl.HOST.value}])


class TestBash:
    """bash: вывод команды блоком, код возврата строкой под ним."""

    def test_echo(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="bash",
            arguments={"command": f"echo {ProbeText.BASH_ECHO.value}"},
            view=ScriptCall(arg="command", lang="bash"),
        )
        expect = ToolExpect(
            output=f"```stdout\n{ProbeText.BASH_ECHO.value}\n```\n\n_exit code: 0_",
            dom=[ProbeText.BASH_ECHO.value, "exit code: 0"],
        )
        feed.call(call, expect)

    def test_failed_command_is_crossed(self, feed: ToolFeed) -> None:
        """Ненулевой код возврата — крест в названии и stderr вместо stdout."""
        call = ToolCall(
            tool="bash",
            arguments={"command": f"echo {ProbeText.BASH_STDERR.value} >&2; exit 3"},
            view=ScriptCall(arg="command", lang="bash"),
        )
        expect = ToolExpect(
            mark=StepMark.FAILED,
            output=f"```stderr\n{ProbeText.BASH_STDERR.value}\n```\n\n_exit code: 3_",
            dom=[ProbeText.BASH_STDERR.value, "exit code: 3"],
        )
        feed.call(call, expect)

    def test_stream_button_reaches_the_data_layer(
        self, feed: ToolFeed, stand_db: StandDatabase
    ) -> None:
        """Элемент кнопки потока bash-шага записан в базу: колбэки трасера
        идут в loop приложения, а не в чужой — иначе запись молча терялась."""
        before = stand_db.elements_named(STREAM_ELEMENT)

        call = ToolCall(
            tool="bash",
            arguments={"command": f"echo {ProbeText.BASH_ECHO.value}"},
            view=ScriptCall(arg="command", lang="bash"),
        )
        feed.call(call, ToolExpect(dom=[ProbeText.BASH_ECHO.value]))

        after = stand_db.elements_named(STREAM_ELEMENT)
        if after <= before:
            raise AssertionError(
                f"element {STREAM_ELEMENT} is not stored: was {before}, now {after}\n"
                + feed.stand.tail(60)
            )


class TestDocTools:
    """doc: liteparse читает pdf из образа пользователя."""

    def test_read_document(self, feed: ToolFeed, probe_pdf: str) -> None:
        call = ToolCall(
            tool="read_document",
            arguments={"path": ProbeFile.PDF.value, "pages": "1-2", **OcrArgs.of()},
        )
        text = "\n\n".join(SamplePdf.PAGES)
        feed.call(call, ToolExpect.of(TextResult(text=text), dom=list(SamplePdf.PAGES)))

    def test_document_outline(self, feed: ToolFeed, probe_pdf: str) -> None:
        call = ToolCall(
            tool="document_outline",
            arguments={"path": ProbeFile.PDF.value, **OcrArgs.of()},
        )
        rows: list[dict[str, Any]] = []
        for number, text in enumerate(SamplePdf.PAGES, start=1):
            rows.append(
                {
                    "page": number,
                    "width": 300.0,
                    "height": 300.0,
                    "chars": len(text),
                    "items": 1,
                }
            )
        result = TableResult(rows=rows, note=f"{ProbeFile.PDF.value}: pages 2")
        feed.call(call, ToolExpect.of(result, dom=["300.0", "pages 2"]))

    def test_search_document(self, feed: ToolFeed, probe_pdf: str) -> None:
        call = ToolCall(
            tool="search_document",
            arguments={
                "path": ProbeFile.PDF.value,
                "query": ProbeText.PDF_QUERY.value,
                **OcrArgs.of(),
            },
        )
        rows = [
            {
                "page": 1,
                "x": 20.0,
                "y": 81.1,
                "width": 140.1,
                "height": 23.4,
                "snippet": SamplePdf.PAGES[0],
            },
            {
                "page": 2,
                "x": 20.0,
                "y": 81.1,
                "width": 239.0,
                "height": 23.4,
                "snippet": SamplePdf.PAGES[1],
            },
        ]
        result = TableResult(rows=rows, note=f"{ProbeFile.PDF.value}: matches 2")
        feed.call(call, ToolExpect.of(result, dom=[*SamplePdf.PAGES, "matches 2"]))


class TestWebTools:
    """web: страницы фейкового сервера по whitelist-соединению stand."""

    def test_connection_list(self, feed: ToolFeed) -> None:
        call = ToolCall(tool="web_connection_list")
        expect = ToolExpect.of(_web_connections(), dom=["stand", StandUrl.HOST.value])
        feed.call(call, expect)

    def test_fetch_raw_html(self, feed: ToolFeed, llm_port: int) -> None:
        url = StandUrl.of(llm_port, FakePage.HTML.route.value)
        call = ToolCall(
            tool="web_fetch_page",
            arguments={
                "url": url,
                "connection_name": "stand",
                "as_markdown": False,
                "line_offset": 0,
                "line_count": 50,
            },
        )
        result = TextResult(
            text=FakePage.HTML.value, language="html", note=f"url={url}; lines 1-1 of 1"
        )
        feed.call(call, ToolExpect.of(result, dom=["stand page", "lines 1-1 of 1"]))

    def test_fetch_line_window(self, feed: ToolFeed, llm_port: int) -> None:
        url = StandUrl.of(llm_port, FakePage.LINES.route.value)
        call = ToolCall(
            tool="web_fetch_page",
            arguments={
                "url": url,
                "connection_name": "stand",
                "as_markdown": False,
                "line_offset": 1,
                "line_count": 1,
            },
        )
        lines = FakePage.LINES.value.splitlines()
        result = TextResult(
            text=lines[1], language="html", note=f"url={url}; lines 2-2 of 3"
        )
        feed.call(call, ToolExpect.of(result, dom=[lines[1], "lines 2-2 of 3"]))

    def test_grep_match(self, feed: ToolFeed, llm_port: int) -> None:
        url = StandUrl.of(llm_port, FakePage.LINES.route.value)
        grep = GrepCase(
            text=FakePage.LINES.value,
            pattern="line two",
            source=f"url={url}",
            clip_chars=2000,
        )
        call = ToolCall(
            tool="web_grep_page",
            arguments={
                "url": url,
                "connection_name": "stand",
                "as_markdown": False,
                **grep.arguments(),
            },
        )
        feed.call(
            call, ToolExpect.of(grep.result(), dom=["2: stand line two", "matches: 1"])
        )

    def test_grep_without_matches(self, feed: ToolFeed, llm_port: int) -> None:
        url = StandUrl.of(llm_port, FakePage.LINES.route.value)
        grep = GrepCase(
            text=FakePage.LINES.value,
            pattern=ProbeText.NOTHING.value,
            source=f"url={url}",
            clip_chars=2000,
        )
        call = ToolCall(
            tool="web_grep_page",
            arguments={
                "url": url,
                "connection_name": "stand",
                "as_markdown": False,
                **grep.arguments(),
            },
        )
        feed.call(call, ToolExpect.of(grep.result(), dom=["no matches found"]))


class TestConfluenceTools:
    """confluence: живой сервер; ожидания считаются REST'ом тем же профилем."""

    def test_spaces(self, feed: ToolFeed, confluence_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="confluence_spaces",
            arguments={
                "pattern": confluence_page.space_key,
                "space_type": "global",
                "limit": 200,
            },
        )
        row = {
            "key": confluence_page.space_key,
            "name": confluence_page.space_name,
            "type": confluence_page.space_type,
        }
        result = TableResult(rows=[row])
        feed.call(call, ToolExpect.of(result, dom=[confluence_page.space_name]))

    def test_search(self, feed: ToolFeed, confluence_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="confluence_search",
            arguments={
                "query": ProbeText.CONFLUENCE_QUERY.value,
                "limit": ConfluenceSite.SEARCH_LIMIT,
                "snippet_chars": 100,
                "offset": 0,
            },
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row("page_id", "title", "space_key", "url", "excerpt"),
                TablePattern.cells(
                    confluence_page.page_id, re.escape(confluence_page.title)
                ),
                r"^_rows 1-\d+(?: of \d+)?; (?:end of result|next offset=\d+)_$",
            ],
            dom=[confluence_page.page_id, confluence_page.title],
        )
        feed.call(call, expect)

    def test_fetch(self, feed: ToolFeed, confluence_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="confluence_fetch",
            arguments={"page_id": confluence_page.page_id, "as_markdown": True},
        )
        result = TextResult(text=confluence_page.markdown)
        feed.call(call, ToolExpect.of(result, dom=[confluence_page.word]))

    def test_grep(
        self,
        feed: ToolFeed,
        confluence_page: ConfluencePage,
        confluence_site: ConfluenceSite,
    ) -> None:
        grep = GrepCase(
            text=confluence_page.markdown,
            pattern=confluence_page.word,
            source=f"page_id={confluence_page.page_id}",
            clip_chars=confluence_site.max_text_chars,
        )
        call = ToolCall(
            tool="confluence_grep",
            arguments={
                "page_id": confluence_page.page_id,
                "as_markdown": True,
                **grep.arguments(),
            },
        )
        feed.call(call, ToolExpect.of(grep.result(), dom=[confluence_page.word]))


class TestIngestTools:
    """ingest: страница уезжает в базу знаний стенда; итог — строка счётчиков."""

    def test_index_pages(self, indexed_page: ConfluencePage) -> None:
        """Сам вызов проверен фикстурой: здесь важен факт индексации."""
        if not indexed_page.page_id:
            raise AssertionError("indexed page has no id")

    def test_index_cql_skips_unchanged(
        self, feed: ToolFeed, indexed_page: ConfluencePage
    ) -> None:
        call = ToolCall(
            tool="confluence_index_cql",
            arguments={"cql": f"id = {indexed_page.page_id}", "prune_missing": False},
        )
        row = {
            "collection": "kb_confluence",
            "indexed": 0,
            "skipped_unchanged": 1,
            "pruned": 0,
            "failed": 0,
        }
        result = TableResult(rows=[row])
        feed.call(
            call,
            ToolExpect.of(result, dom=["skipped_unchanged", "kb_confluence"]),
            timeout_sec=INGEST_TIMEOUT_SEC,
        )

    def test_index_unknown_space_fails(
        self, feed: ToolFeed, confluence_site: ConfluenceSite
    ) -> None:
        """Отказ тела приходит конвертом: текст ошибки — как у httpx."""
        call = ToolCall(
            tool="confluence_index_spaces",
            arguments={
                "space_keys": [ProbeText.NO_SPACE.value],
                "prune_missing": False,
                "force_update": False,
            },
        )
        url = confluence_site.base_url + ConfluenceRest.space_pages_path(
            ProbeText.NO_SPACE.value
        )
        message = (
            f"tool failed 'confluence_index_spaces': PayloadFailureError: "
            f"Client error '404 ' for url '{url}'\n"
            "For more information check: "
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404"
        )
        result = ErrorResult(message=message, error_kind="ingest_request_failed")
        expect = ToolExpect(
            mark=StepMark.FAILED,
            output=ToolResultMarkdown(result).render(),
            dom=["Error:", ProbeText.NO_SPACE.value],
            log_errors=True,
        )
        feed.call(call, expect, timeout_sec=INGEST_TIMEOUT_SEC)

    def test_attachment(
        self, feed: ToolFeed, confluence_attachment: ConfluenceAttachment
    ) -> None:
        call = ToolCall(
            tool="confluence_attachment",
            arguments={
                "page_id": confluence_attachment.page_id,
                "filename": confluence_attachment.filename,
                **OcrArgs.of(),
            },
        )
        patterns: list[str] = []
        for word in confluence_attachment.words:
            patterns.append(re.escape(word))

        expect = ToolExpect(patterns=patterns, dom=list(confluence_attachment.words))
        feed.call(call, expect, timeout_sec=INGEST_TIMEOUT_SEC)


class TestKbTools:
    """kb: поиск по проиндексированной странице и пустая выдача."""

    COLUMNS: ClassVar[tuple[str, ...]] = (
        "distance",
        "format_content",
        "page_title",
        "source_url",
        "parent_url",
        "doc_type",
        "page",
        "anchor",
        "page_id",
        "version",
        "heading_path",
        "space",
    )

    def _hit_patterns(self, page: ConfluencePage) -> list[str]:
        return [
            TablePattern.row(*self.COLUMNS),
            TablePattern.cells(re.escape(page.title)),
            TablePattern.cells(page.page_id),
        ]

    def test_fts_search(self, feed: ToolFeed, indexed_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="kb_fts_search",
            arguments={"query": indexed_page.word, "top_k": 3},
        )
        expect = ToolExpect(
            patterns=self._hit_patterns(indexed_page),
            dom=[indexed_page.title, indexed_page.page_id],
        )
        feed.call(call, expect)

    def test_vector_search(self, feed: ToolFeed, indexed_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="kb_vector_search",
            arguments={"query": indexed_page.word, "top_k": 3},
        )
        expect = ToolExpect(
            patterns=self._hit_patterns(indexed_page),
            dom=[indexed_page.title, indexed_page.page_id],
        )
        feed.call(call, expect)

    def test_fts_nothing_found(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="kb_fts_search",
            arguments={"query": ProbeText.NOTHING.value, "top_k": 1},
        )
        result = TableResult(rows=[], note="nothing found")
        feed.call(call, ToolExpect.of(result, dom=["(no rows)", "nothing found"]))


class TestPgTools:
    """pg: соединение main стенда, своя таблица, каждый инструмент по разу."""

    def test_connection_list(self, feed: ToolFeed) -> None:
        call = ToolCall(tool="pg_connection_list")
        feed.call(call, ToolExpect.of(_connections("main"), dom=["main"]))

    def test_query_creates_table(self, probe_table: str) -> None:
        """Сам вызов проверен фикстурой: набор команд одной транзакцией."""
        if probe_table != ProbeSql.TABLE.value:
            raise AssertionError(f"probe table is odd: {probe_table}")

    def test_query_update(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_query",
            arguments={"connection_name": "main", "sql": ProbeSql.UPDATE.value},
            view=ScriptCall(arg="sql", lang="sql"),
        )
        result = AffectedSqlResult(affected_rows=1, status="UPDATE 1")
        feed.call(call, ToolExpect.of(result, dom=["rows affected: 1 (UPDATE 1)"]))

    def test_query_select(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_query",
            arguments={"connection_name": "main", "sql": ProbeSql.SELECT.value},
            view=ScriptCall(arg="sql", lang="sql"),
        )
        result = TableResult(
            rows=[{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
        )
        feed.call(call, ToolExpect.of(result, dom=["alpha", "beta"]))

    def test_list_tables(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_list_tables",
            arguments={
                "connection_name": "main",
                "pg_schema": ProbeSql.SCHEMA.value,
                "table_pattern": probe_table,
                **RowWindowArgs.of(),
            },
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row(
                    "schema",
                    "table_name",
                    "kind",
                    "approx_rows",
                    "owner",
                    "total_bytes",
                    "comment",
                ),
                TablePattern.row(
                    ProbeSql.SCHEMA.value,
                    probe_table,
                    "r",
                    r"-?\d+",
                    ProbeSql.CH_USER.value,
                    r"\d+",
                    "",
                ),
                r"^_rows 1-1; end of result_$",
            ],
            dom=[probe_table, "rows 1-1; end of result"],
        )
        feed.call(call, expect)

    def test_describe_table(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_describe_table",
            arguments={
                "connection_name": "main",
                "table": probe_table,
                "pg_schema": ProbeSql.SCHEMA.value,
                **RowWindowArgs.of(),
            },
        )
        rows = [
            self._column(1, "id", "integer", nullable=False, primary_key=True),
            self._column(2, "name", "text", nullable=False, primary_key=False),
            self._column(3, "note", "text", nullable=True, primary_key=False),
        ]
        result = TableResult(rows=rows, note="rows 1-3; end of result")
        feed.call(
            call, ToolExpect.of(result, dom=["column_name", "integer", "rows 1-3"])
        )

    @staticmethod
    def _column(
        position: int, name: str, kind: str, *, nullable: bool, primary_key: bool
    ) -> dict[str, Any]:
        return {
            "schema": ProbeSql.SCHEMA.value,
            "position": position,
            "column_name": name,
            "type": kind,
            "nullable": nullable,
            "default_expression": None,
            "identity": "",
            "generated": "",
            "primary_key": primary_key,
            "comment": None,
        }

    def test_copy(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_copy",
            arguments={"connection_name": "main", "sql": ProbeSql.COPY.value},
            view=ScriptCall(arg="sql", lang="sql"),
        )
        result = TextResult(text=ProbeSql.COPY_TEXT.value, language="csv")
        feed.call(call, ToolExpect.of(result, dom=["id,name", "1,alpha", "2,beta"]))


class TestChTools:
    """ch: соединение main стенда под kerberos-учёткой приложения."""

    def test_connection_list(self, feed: ToolFeed) -> None:
        call = ToolCall(tool="ch_connection_list")
        feed.call(call, ToolExpect.of(_connections("main"), dom=["main"]))

    def test_query(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ch_query",
            arguments={"sql": ProbeSql.CH_SELECT.value, "connection_name": "main"},
            view=ScriptCall(arg="sql", lang="sql"),
        )
        result = TableResult(rows=[{"who": ProbeSql.CH_USER.value, "a": 1}])
        feed.call(call, ToolExpect.of(result, dom=[ProbeSql.CH_USER.value]))

    def test_describe_table(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ch_describe_table",
            arguments={
                "connection_name": "main",
                "table": ProbeSql.CH_ONE.value,
                "ch_database": ProbeSql.CH_SYSTEM.value,
                **RowWindowArgs.of(),
            },
        )
        row = {
            "name": "dummy",
            "type": "UInt8",
            "default_kind": "",
            "default_expression": "",
            "comment": "",
        }
        result = TableResult(rows=[row], note="rows 1-1; end of result")
        feed.call(call, ToolExpect.of(result, dom=["dummy", "UInt8"]))

    def test_list_tables(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ch_list_tables",
            arguments={
                "connection_name": "main",
                "ch_database": ProbeSql.CH_SYSTEM.value,
                **RowWindowArgs.of(max_rows=2),
            },
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row("database", "table", "engine", "total_rows"),
                TablePattern.row(
                    ProbeSql.CH_SYSTEM.value,
                    "aggregate_function_combinators",
                    "SystemAggregateFunctionCombinators",
                    "",
                ),
                r"^_rows 1-2; more rows available, next offset=2_$",
            ],
            dom=["aggregate_function_combinators", "next offset=2"],
        )
        feed.call(call, expect)


class TestCanvasTools:
    """Тулы ленты без песочницы: диаграмма, панель, вложение файла в чат."""

    def test_diagram_save(self, saved_diagram: DiagramProbe) -> None:
        """Сам вызов проверен фикстурой: здесь важен путь сохранённого файла."""
        if ProbeDiagram.NAME.value not in saved_diagram.path:
            raise AssertionError(f"diagram path is odd: {saved_diagram.path}")

    def test_canvas_open_renders_diagram(
        self,
        canvas_feed: ToolFeed,
        saved_diagram: DiagramProbe,
        stand_db: StandDatabase,
    ) -> None:
        before = stand_db.elements_named(CANVAS_ELEMENT)

        call = ToolCall(tool="canvas_open", arguments={"path": saved_diagram.path})
        label = f"diagram rendered: {ProbeDiagram.NAME.value}"
        canvas_feed.call(call, ToolExpect(output=label, dom=[label]))

        after = stand_db.elements_named(CANVAS_ELEMENT)
        if after <= before:
            raise AssertionError(
                f"element {CANVAS_ELEMENT} is not stored: was {before}, now {after}"
            )

    def test_canvas_open_outside_thread_is_refused(
        self, canvas_feed: ToolFeed, saved_diagram: DiagramProbe
    ) -> None:
        call = ToolCall(
            tool="canvas_open", arguments={"path": ProbeText.OUTSIDE_PATH.value}
        )
        name = Path(ProbeText.OUTSIDE_PATH.value).name
        expected = f"/workspace/{saved_diagram.thread_id}/{{mermaid|upload}}/{name}"
        outside = ProbeText.OUTSIDE_PATH.value
        message = (
            f"file is outside the thread attachments dir: {outside!r}; "
            f"expected {expected!r}"
        )
        result = ErrorResult(message=message, error_kind="bad_path")
        canvas_feed.call(
            call,
            ToolExpect.of(result, dom=["Error:", "outside the thread attachments dir"]),
        )

    def test_send_file(
        self, canvas_feed: ToolFeed, saved_diagram: DiagramProbe
    ) -> None:
        call = ToolCall(tool="send_file", arguments={"path": saved_diagram.path})
        result = TextResult(
            text=f"file attached to the chat: {ProbeDiagram.NAME.value}"
        )
        canvas_feed.call(
            call,
            ToolExpect.of(
                result, dom=[f"file attached to the chat: {ProbeDiagram.NAME.value}"]
            ),
        )


class TestStreamLogsTools:
    """stream_logs: журналы вызовов песочницы; чужой тред чистится, свой — нет."""

    def test_usage_lists_the_bash_thread(self, feed: ToolFeed, probe_pdf: str) -> None:
        call = ToolCall(tool="stream_logs_usage")
        expect = ToolExpect(
            patterns=[
                UsagePattern.VOLUME,
                UsagePattern.THREADS,
                UsagePattern.thread(probe_pdf),
            ],
            dom=["volume:", probe_pdf],
        )
        feed.call(call, expect)

    def test_cleanup_missing_thread_is_refused(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="stream_logs_cleanup",
            arguments={"thread_id": ProbeText.MISSING_THREAD.value},
        )
        result = ErrorResult(
            message=f"no journals found for thread {ProbeText.MISSING_THREAD.value}",
            error_kind="thread_not_found",
        )
        feed.call(call, ToolExpect.of(result, dom=["Error:", "no journals found"]))

    def test_cleanup_purges_the_bash_thread(
        self, feed: ToolFeed, probe_pdf: str
    ) -> None:
        """Идёт последним: журнал bash-треда нужен тесту usage."""
        call = ToolCall(tool="stream_logs_cleanup", arguments={"thread_id": probe_pdf})
        expect = ToolExpect(
            patterns=[
                f"^journals of thread {re.escape(probe_pdf)} deleted, freed \\d+ bytes$"
            ],
            dom=[f"journals of thread {probe_pdf} deleted"],
        )
        feed.call(call, expect)


class TestCoverage:
    """Прогон вызвал каждый инструмент, который стенд отдаёт модели."""

    def test_every_stand_tool_is_called(self, feed: ToolFeed, llm_port: int) -> None:
        call = ToolCall(tool="web_connection_list")
        feed.call(call, ToolExpect.of(_web_connections()))

        response = httpx.get(
            StandUrl.of(llm_port, FakeRoute.REQUESTS.value), timeout=5.0
        )
        response.raise_for_status()
        requests = response.json()["requests"]
        if not requests:
            raise AssertionError("fake llm recorded no requests")

        offered: set[str] = set()
        for spec in requests[-1].get("tools") or []:
            offered.add(str(spec["function"]["name"]))

        missing = offered - Coverage.called
        if missing:
            raise AssertionError(f"tools without a stand call: {sorted(missing)}")

        unknown = Coverage.called - offered
        if unknown:
            raise AssertionError(
                f"called tools the stand does not offer: {sorted(unknown)}"
            )
