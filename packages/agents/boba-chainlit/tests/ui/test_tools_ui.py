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
import io
import json
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from chat_ui import ChatOpener

from boba.canvas.diagram import DiagramPrompt
from boba.config import bind
from boba.confluence.html import MarkdownRender, SectionsRender
from boba.confluence.models import (
    ConfluenceSpaceItem,
    PageSections,
    PageTextSection,
    TableShape,
)
from boba.confluence.parsing import JsonNode
from boba.confluence.rest import CflRestBuilder
from boba.doc.config import DocConfig
from boba.doc.document import DisabledOcr, DocumentHint, PageWindow
from boba.doc.router import DocumentRouter
from boba.runtime.config import AppLayers
from boba.stand.edm import Asset, EdmSources
from boba.stand.ix_index import IxPage
from boba.stand.site import Stand, StandLayers
from boba.stand.stream_sinks import SinkSources, StreamSinks
from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.database import StandDatabase, StandOracle
from boba.stand.ui.fake_llm import FakePage, FakeRoute, ScenarioName
from boba.stand.ui.socket_log import ChatEvent, StepField
from boba.stand.ui.stand import (
    REPO_ROOT,
    StandApp,
    StandConfig,
    StandPaths,
    StandProcess,
    StandUrl,
    free_port,
)
from boba.text.grep import GrepLimits, TextGrep
from boba.tool.canvas.tools import CanvasPrompt
from boba.tool.confluence.ingest_tools import IngestToolConfig
from boba.tool.confluence.tools import ConfluenceToolsConfig, CqlQuery
from boba.tool.kb.search import ConfluenceCollection
from boba.toolkit.calls import ToolIntent
from boba.toolkit.result import (
    CanvasResult,
    ErrorResult,
    FieldLines,
    FileResult,
    MarkdownResult,
    SqlResult,
    SqlStatement,
    TableResult,
    ToolResult,
)
from boba.transport.http import HttpxAuth
from boba.transport.http.connection import HttpConnection

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 300.0
"""Подъём стенда с песочницей: восемь зигот, у kb — прогрев эмбеддера."""

TURN_TIMEOUT_SEC = 180.0

INGEST_TIMEOUT_SEC = 600.0
"""Индексация страницы: эмбеддер e5-large в песочнице считает на CPU."""

STREAM_ELEMENT = "CanvasStream"
"""Имя элемента кнопки живого вывода на шаге инструмента песочницы."""

CANVAS_ELEMENT = "CanvasView"
"""Имя элемента карточки диаграммы в ленте."""

"""Имя элемента ссылки на страницу каталога в ленте."""


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
    COPY_TABLE = "ui_probe_copy"
    COPY_TARGET = (
        "drop table if exists public.ui_probe_copy; "
        "create table public.ui_probe_copy (id integer, name text, note text)"
    )
    CH_SELECT = "select currentUser() as who, 1 as a"
    CH_USER = "boba-svc"
    CH_SYSTEM = "system"
    CH_ONE = "one"
    ORA_SELECT = "select 1 as a from dual"
    ORA_SYSTEM = "SYS"
    ORA_DUAL = "DUAL"


class IxProbe:
    """Страницы Confluence, которыми стенд засевает схему ix: заказы и склады,
    чтобы векторный поиск различал их по смыслу."""

    SURFACE: ClassVar[str] = "cfl_page"
    PAGES: ClassVar[tuple[IxPage, ...]] = (
        IxPage(
            content_id="9001",
            title="Регламент выгрузки заказов",
            body=(
                "# Выгрузка заказов\n\n"
                "Ежедневная выгрузка заказов в хранилище начинается после "
                "закрытия операционного дня и занимает около часа."
            ),
        ),
        IxPage(
            content_id="9002",
            title="Справочник складов",
            body=(
                "# Склады\n\n"
                "Остатки товаров на складах обновляются каждый час из учётной "
                "системы логистики."
            ),
        ),
    )


class RowWindowArgs:
    """Окно выдачи каталожных инструментов: одно на все вызовы стенда."""

    OFFSET: ClassVar[int] = 0
    LIMIT: ClassVar[int] = 10

    @classmethod
    def of(cls, limit: int = LIMIT) -> dict[str, Any]:
        return {"offset": cls.OFFSET, "limit": limit}


class OcrArgs:
    """Параметры чтения документов: OCR выключен, как у текстовых pdf."""

    PAGE_GLUE: ClassVar[str] = "\n\n"

    @staticmethod
    def of() -> dict[str, Any]:
        return {"ocr_enabled": False}

    @classmethod
    def text_of(cls, content: bytes, filename: str) -> str:
        """Текст документа теми же ридерами boba-doc, что и у инструментов."""
        config = DocConfig(text_encodings=("utf-8",))
        router = DocumentRouter(config, DisabledOcr())
        hint = DocumentHint(filename=filename)
        with router.open(io.BytesIO(content), hint) as document:
            texts: list[str] = []
            for page in document.pages(PageWindow.whole()):
                texts.append(page.text)

        return cls.PAGE_GLUE.join(texts)


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
    code: str = ""
    """Аргумент, объявленный тулом как код: рисуется блоком с языком."""
    language: str = ""
    label: str = ""
    """Своя подпись вызова: различает вызовы одного инструмента в одном ходе."""
    hidden: Sequence[str] = ()
    """Аргументы, которых лента во входе шага не рисует: каналы насосов."""

    @property
    def intent(self) -> str:
        """Подпись вызова: своя либо та, что фейк дописывает за отсутствием."""
        if self.label:
            return self.label

        return f"stand call of {self.tool}"

    def request(self) -> dict[str, Any]:
        """Вызов для сценария фейка; своя подпись едет полем intent."""
        arguments = dict(self.arguments)
        if self.label:
            arguments[ToolIntent.NAME] = self.label

        return {"name": self.tool, "arguments": arguments}

    def message(self) -> str:
        request = json.dumps(self.request(), ensure_ascii=False)
        return f"{ScenarioName.CALL.value} {request}"

    def expected_input(self) -> str | None:
        """Вход шага, каким его рисует лента из аргументов без подписи."""
        if not self.arguments:
            return None

        blocks: list[str] = []
        for name, value in self.arguments.items():
            if name in self.hidden:
                continue

            if name == self.code:
                shown = MarkdownResult(
                    text=str(value).strip("\n"), language=self.language
                )
                blocks.append(shown.chat_view().markdown)
                continue

            if isinstance(value, str) and not value:
                continue

            blocks.append(FieldLines.line(name, value))

        return "\n\n".join(blocks)


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
            output=result.chat_view().markdown,
            dom=dom,
        )


@dataclass(frozen=True)
class ToolStep:
    """Шаг инструмента, каким его получила вкладка."""

    payload: Mapping[str, Any]
    dom_text: str
    dividers: Sequence[str] = ()
    """Толщина верхней границы каждого разделителя вызова и результата."""

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
        self._check_divider()

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

    def _check_divider(self) -> None:
        """Вызов и результат разделены одной видимой линией."""
        if not self._step.input:
            return

        if not self._step.output:
            return

        if len(self._step.dividers) != 1:
            self._fail(f"expected one input/output divider, got {self._step.dividers}")

        if self._step.dividers[0] in ("", "0px"):
            self._fail(f"the divider has no border: {self._step.dividers}")

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

    DIVIDER: ClassVar[str] = "[data-step-io-divider]"

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
        dividers = node.locator(self.DIVIDER).evaluate_all(
            "nodes => nodes.map(n => getComputedStyle(n).borderTopWidth)"
        )
        step = ToolStep(payload=payload, dom_text=node.inner_text(), dividers=dividers)

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

    def call_group(
        self,
        calls: Sequence[tuple[ToolCall, ToolExpect]],
        timeout_sec: float = TURN_TIMEOUT_SEC,
    ) -> list[ToolStep]:
        """Один ход модели со всеми вызовами разом — так модель связывает
        насосы каналами; шаг каждого вызова сверяется со своим ожиданием."""
        requests: list[dict[str, Any]] = []
        for call, _ in calls:
            Coverage.called.add(call.tool)
            requests.append(call.request())

        log_mark = self.stand.log_lines()
        message = json.dumps(requests, ensure_ascii=False)
        self.chat.ask(f"{ScenarioName.CALL.value} {message}")
        self.chat.await_idle(timeout_sec=timeout_sec)
        self.chat.expand_last_run()

        steps: list[ToolStep] = []
        for call, expect in calls:
            step = self._group_step(call)
            StepCheck(step, call, expect).run()
            steps.append(step)

        complaints = self.stand.complaints(since_line=log_mark)
        if complaints:
            raise AssertionError(
                "stream group left errors in the stand log:\n"
                + "\n".join(complaints[:10])
            )

        return steps

    def _group_step(self, call: ToolCall) -> ToolStep:
        title = f" {call.tool} · {call.intent}"

        payload: Mapping[str, Any] | None = None
        for candidate in self.chat.log.steps_of_type(StepKind.TOOL.value):
            name = str(candidate.get(StepField.NAME.value) or "")
            if title in name:
                payload = candidate

        if payload is None:
            raise AssertionError(
                f"tool {call.tool}: no step {title!r} in the socket log\n"
                f"{self.chat.log.describe()}\n{self.stand.tail(60)}"
            )

        node = self.chat.expand_tool_titled(title)
        dividers = node.locator(self.DIVIDER).evaluate_all(
            "nodes => nodes.map(n => getComputedStyle(n).borderTopWidth)"
        )
        return ToolStep(payload=payload, dom_text=node.inner_text(), dividers=dividers)

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
    table_shape: TableShape

    WORD: ClassVar[str] = r"[^\W\d_]{6,}"
    """Слово для grep и поиска: только буквы, чтобы regex и tsquery не спорили."""

    @property
    def markdown(self) -> str:
        answer = MarkdownRender({"html": self.html, "heading_style": "ATX"}).run()
        return str(answer["markdown"])

    @property
    def indexed_text(self) -> str:
        """Текст секций страницы: ровно то, что ingest кладёт в базу знаний."""
        request = {
            "html": self.html,
            "title": self.title,
            "page_id": self.page_id,
            "table_shape": self.table_shape,
        }
        parsed = PageSections.model_validate(SectionsRender(request).run())
        parts: list[str] = []
        for section in parsed.sections:
            if not isinstance(section, PageTextSection):
                continue

            parts.append(section.content)

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
    """Вложение живого Confluence и его текст, разобранный ридерами boba-doc.

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
    PAGE_ID_IN_URL: ClassVar[str] = r"(?:pageId=|/pages/)(\d+)"

    def __init__(self, config: ConfluenceToolsConfig, table_shape: TableShape) -> None:
        self._config = config
        self._table_shape = table_shape
        self._rest = CflRestBuilder()
        profile = config.confluence
        self._profile = profile
        self._client = httpx.Client(
            timeout=profile.timeout_sec,
            verify=profile.ssl_verify,
            follow_redirects=True,
            auth=HttpxAuth().of(profile),
        )

    @classmethod
    def load(cls) -> ConfluenceSite:
        built = AppLayers.compose(StandPaths.BASE_CONFIG.under(REPO_ROOT))
        config = bind(
            built, path=ConfluenceToolsConfig.SECTION, model=ConfluenceToolsConfig
        )
        ingest = bind(built, path=IngestToolConfig.SECTION, model=IngestToolConfig)
        return cls(config, ingest.table_shape)

    @property
    def profile(self) -> HttpConnection:
        return self._profile

    def space(self, key: str) -> ConfluenceSpaceItem:
        """Спейс по ключу тем же REST-адресом, что у инструментов."""
        return ConfluenceSpaceItem.model_validate(
            self.get_json(self._rest.space_path(key))
        )

    @property
    def max_text_chars(self) -> int:
        return self._config.max_text_chars

    def url_of(self, path: str) -> str:
        return str(self._profile.url_of(path))

    def close(self) -> None:
        self._client.close()

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({401, 429, 500, 502, 503})
    RETRIES: ClassVar[int] = 3
    RETRY_SEC: ClassVar[float] = 2.0

    def get_json(self, path: httpx.URL) -> dict[str, Any]:
        """Публичный Confluence изредка отвечает 401/5xx на ровном месте: повторяем."""
        attempt = 0
        while True:
            attempt += 1
            response = self._client.get(self.url_of(str(path)))
            if response.status_code not in self.RETRY_STATUSES:
                response.raise_for_status()
                return response.json()

            if attempt >= self.RETRIES:
                response.raise_for_status()

            # 401 приходит на сессионную cookie после серии запросов: сбрасываем её
            self._client.cookies.clear()
            time.sleep(self.RETRY_SEC)

    def get_bytes(self, path: str) -> bytes:
        response = self._client.get(self.url_of(path))
        response.raise_for_status()
        return response.content

    def find_page(self, query: str) -> ConfluencePage:
        """Самая короткая непустая страница из выдачи того же CQL, что у тула."""
        cql = CqlQuery(query, None).render()
        path = self._rest.cql_search_path(
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
            if str(hit.get("type") or "") != "page":
                continue

            title = str(hit.get("title") or "")

            space = hit.get("space")
            if not isinstance(space, dict):
                continue

            if str(space.get("type") or "") != "global":
                continue

            html = JsonNode(hit).body_html("view")
            if len(html) < self.MIN_HTML_CHARS:
                continue

            yield ConfluencePage(
                page_id=str(hit.get("id") or ""),
                title=title,
                space_key=str(space.get("key") or ""),
                space_name=str(space.get("name") or ""),
                space_type=str(space.get("type") or ""),
                html=html,
                table_shape=self._table_shape,
            )

    def find_attachment(self, query: str) -> ConfluenceAttachment:
        """Вложение .docx из поиска; текст считается теми же ридерами boba-doc."""
        cql = CqlQuery(query, None).render()
        path = self._rest.cql_search_path(cql, limit=self.ATTACHMENT_LIMIT, start=0)
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
            text = OcrArgs.text_of(content, title)
            return ConfluenceAttachment(page_id=page_id, filename=title, text=text)

        pytest.skip("Confluence search returned no .docx attachment")

    def _attachment_link(self, page_id: str, filename: str) -> str:
        path = self._rest.page_fetch_path(page_id, body_format=self._config.body_format)
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

    def arguments(self, *, as_markdown: bool) -> dict[str, Any]:
        """Аргументы grep-тула в порядке его подписи: так рисуется вход шага."""
        return {
            "pattern": self.pattern,
            "as_markdown": as_markdown,
            "case_insensitive": False,
            "context": self.CONTEXT,
            "limit": self.LIMIT,
            "fixed_string": True,
        }

    def result(self) -> MarkdownResult:
        compiled = TextGrep.compile_pattern(
            self.pattern, fixed_string=True, case_insensitive=False
        )
        limits = GrepLimits(
            context=self.CONTEXT, limit=self.LIMIT, clip_chars=self.clip_chars
        )
        report = TextGrep.report(self.text, compiled, limits, self.source)
        return MarkdownResult(
            text=report.render(), language=report.LANG, note=report.note
        )


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
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-sandbox",
        sandbox=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "sandbox-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    database = StandDatabase(StandApp.CHAINLIT, stand_database)
    try:
        # роли стенда в таблице появляются на старте: гранты кладутся после него
        database.seed_connections(llm_port)
        database.seed_oracle()
        database.seed_edm()
        database.seed_stream_sinks()
        yield process
    finally:
        process.stop()
        database.drop_edm()
        database.drop_stream_sinks()


@pytest.fixture(scope="module")
def stream_sinks(sandbox_stand: StandProcess, stand_database: str) -> StreamSinks:
    """База ClickHouse и схема Oracle приёмников насосов, заведённые стендом."""
    return StandDatabase(StandApp.CHAINLIT, stand_database).stream_sinks()


@pytest.fixture(scope="module")
def ix_nodes(sandbox_stand: StandProcess, stand_database: str) -> list[int]:
    """Страницы IxProbe в схеме ix базы стенда с индексами: id их node."""
    database = StandDatabase(StandApp.CHAINLIT, stand_database)
    return database.seed_ix(IxProbe.PAGES)


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
                f"<<'B64'\n{SamplePdf.base64()}\nB64\ntest -s {ProbeFile.PDF.value}"
            ),
        },
        code="command",
        language="bash",
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
        tool="confluence_index_page",
        arguments={
            "page_id": confluence_page.page_id,
            "attachments": True,
        },
    )
    expect = ToolExpect(
        patterns=[
            TablePattern.row(
                "kind",
                "found",
                "indexed",
                "unchanged",
                "skipped",
                "failed",
                "deleted",
                "chunks",
                "chunks_deleted",
                "skipped_reasons",
                "error",
            ),
            TablePattern.cells("pages", "1", r"\d+", r"\d+", "0", "0", "0"),
            f"^_collection: kb_confluence; page_id: {confluence_page.page_id}_$",
        ],
        dom=["kb_confluence", f"page_id: {confluence_page.page_id}"],
    )
    module_feed.call(call, expect, timeout_sec=INGEST_TIMEOUT_SEC)
    return confluence_page


@pytest.fixture(scope="module")
def probe_table(module_feed: ToolFeed) -> str:
    """Таблица стенда создаётся pg_query: набор команд одной транзакцией."""
    call = ToolCall(
        tool="pg_query",
        arguments={
            "connection": "main",
            "sql": ProbeSql.CREATE.value,
            **RowWindowArgs.of(),
        },
        code="sql",
        language="sql",
    )
    result = SqlResult(
        engine="postgres",
        statements=[
            SqlStatement(status="DROP TABLE"),
            SqlStatement(status="CREATE TABLE"),
            SqlStatement(affected_rows=2, status="INSERT 0 2"),
        ],
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
    # вызов без аргументов даёт id треда вкладки: он нужен пути файла
    listing = ToolCall(tool="connection_list")
    step = canvas_feed.call(listing, ToolExpect.of(_connection_catalog()))
    probe = DiagramProbe(thread_id=step.thread_id)

    call = ToolCall(
        tool="diagram_save",
        arguments={"name": ProbeDiagram.NAME.value, "spec": ProbeDiagram.SPEC.value},
        code="spec",
        language="mermaid",
    )
    result = CanvasResult(
        path=probe.path,
        label=ProbeDiagram.NAME.value,
        summary=f"diagram saved: {probe.path}",
        note=DiagramPrompt.SAVED_NOTE.value,
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


def _connection_catalog() -> TableResult:
    """Выдача connection_list: все строки стенда, по виду и имени."""
    stand = Stand.required()
    config_path = StandApp.CHAINLIT.base_config.under(REPO_ROOT)
    built = StandLayers.compose(config_path)
    oracle = StandOracle(built)
    sources = bind(built, path="ix_stand", model=EdmSources)
    edm = sources.demo()[0]
    sinks = StreamSinks(bind(built, path="ix_stand", model=SinkSources))
    listed = (
        ("edm", "clickhouse", edm.clickhouse.host),
        ("main", "clickhouse", stand.ch_host),
        (StandDatabase.SINK_CH, "clickhouse", sinks.ch_connection.host),
        ("main", "oracle", oracle.host),
        (StandDatabase.SINK_ORA, "oracle", sinks.ora_connection.host),
        ("main", "postgres", stand.pg_host),
        ("stand", "web", StandUrl.HOST.value),
    )

    rows: list[dict[str, Any]] = []
    for name, kind, host in listed:
        rows.append({"connection": name, "kind": kind, "host": host, "description": ""})

    return TableResult(rows=rows)


CATALOG_DOM: tuple[str, ...] = (
    "edm",
    "main",
    StandDatabase.SINK_CH,
    StandDatabase.SINK_ORA,
    "stand",
    "postgres",
    "clickhouse",
    "oracle",
    "web",
)
"""Фрагменты каталога, которые обязаны быть видны в раскрытом шаге."""


class TestBash:
    """bash: вывод команды блоком, код возврата строкой под ним."""

    def test_echo(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="bash",
            arguments={"command": f"echo {ProbeText.BASH_ECHO.value}"},
            code="command",
            language="bash",
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
            code="command",
            language="bash",
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
            code="command",
            language="bash",
        )
        feed.call(call, ToolExpect(dom=[ProbeText.BASH_ECHO.value]))

        after = stand_db.elements_named(STREAM_ELEMENT)
        if after <= before:
            raise AssertionError(
                f"element {STREAM_ELEMENT} is not stored: was {before}, now {after}\n"
                + feed.stand.tail(60)
            )


class TestDocTools:
    """doc: ридеры boba-doc читают pdf из образа пользователя."""

    def test_read_document(self, feed: ToolFeed, probe_pdf: str) -> None:
        call = ToolCall(
            tool="read_document",
            arguments={"path": ProbeFile.PDF.value, "pages": "1-2", **OcrArgs.of()},
        )
        text = "\n\n".join(SamplePdf.PAGES)
        feed.call(
            call, ToolExpect.of(MarkdownResult(text=text), dom=list(SamplePdf.PAGES))
        )

    def test_document_outline(self, feed: ToolFeed, probe_pdf: str) -> None:
        call = ToolCall(
            tool="document_outline",
            arguments={"path": ProbeFile.PDF.value, **OcrArgs.of()},
        )
        rows: list[dict[str, Any]] = []
        for number, text in enumerate(SamplePdf.PAGES, start=1):
            rows.append(
                {
                    "number": number,
                    "chars": len(text),
                    "width": 300.0,
                    "height": 300.0,
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
                **RowWindowArgs.of(),
            },
        )
        rows = [
            {
                "page": 1,
                "offset": 0,
                "length": 5,
                "snippet": SamplePdf.PAGES[0],
                "x": 20.280000686645508,
                "y": 195.8800048828125,
                "width": 50.33999443054199,
                "height": 18.479995727539062,
            },
            {
                "page": 2,
                "offset": 14,
                "length": 5,
                "snippet": SamplePdf.PAGES[1],
                "x": 153.70001220703125,
                "y": 195.8800048828125,
                "width": 50.339996337890625,
                "height": 18.479995727539062,
            },
        ]
        result = TableResult(
            rows=rows,
            note="rows 1-2; end of result",
            metadata={"path": ProbeFile.PDF.value, "query": ProbeText.PDF_QUERY.value},
        )
        feed.call(call, ToolExpect.of(result, dom=[*SamplePdf.PAGES, "end of result"]))


class TestWebTools:
    """web: страницы фейкового сервера по whitelist-соединению stand."""

    def test_connection_list(self, feed: ToolFeed) -> None:
        """Общий каталог показывает web-строку stand рядом с остальными."""
        call = ToolCall(tool="connection_list")
        feed.call(call, ToolExpect.of(_connection_catalog(), dom=CATALOG_DOM))

    def test_fetch_raw_html(self, feed: ToolFeed, llm_port: int) -> None:
        url = StandUrl.of(llm_port, FakePage.HTML.route.value)
        call = ToolCall(
            tool="web_fetch_page",
            arguments={
                "url": url,
                "connection": "stand",
                "as_markdown": False,
                "line_offset": 0,
                "line_count": 50,
            },
        )
        result = MarkdownResult(
            text=FakePage.HTML.value, language="html", note=f"url={url}; lines 1-1 of 1"
        )
        feed.call(call, ToolExpect.of(result, dom=["stand page", "lines 1-1 of 1"]))

    def test_fetch_line_window(self, feed: ToolFeed, llm_port: int) -> None:
        url = StandUrl.of(llm_port, FakePage.LINES.route.value)
        call = ToolCall(
            tool="web_fetch_page",
            arguments={
                "url": url,
                "connection": "stand",
                "as_markdown": False,
                "line_offset": 1,
                "line_count": 1,
            },
        )
        lines = FakePage.LINES.value.splitlines()
        result = MarkdownResult(
            text=lines[1], language="text", note=f"url={url}; lines 2-2 of 3"
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
                "connection": "stand",
                **grep.arguments(as_markdown=False),
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
                "connection": "stand",
                **grep.arguments(as_markdown=False),
            },
        )
        feed.call(call, ToolExpect.of(grep.result(), dom=["no matches found"]))


class TestConfluenceTools:
    """confluence: живой сервер; ожидания считаются REST'ом тем же профилем."""

    def test_spaces(
        self,
        feed: ToolFeed,
        confluence_site: ConfluenceSite,
        confluence_page: ConfluencePage,
    ) -> None:
        call = ToolCall(
            tool="confluence_spaces",
            arguments={
                "pattern": confluence_page.space_key,
                "space_type": "global",
                "limit": 200,
            },
        )
        space = confluence_site.space(confluence_page.space_key)
        row = {
            "key": confluence_page.space_key,
            "name": confluence_page.space_name,
            "type": confluence_page.space_type,
            "status": space.status,
            "url": space.url_at(confluence_site.profile),
        }
        result = TableResult(rows=[row])
        feed.call(call, ToolExpect.of(result, dom=[confluence_page.space_name]))

    def test_search(self, feed: ToolFeed, confluence_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="confluence_search",
            arguments={
                "query": ProbeText.CONFLUENCE_QUERY.value,
                "snippet_chars": 100,
                "offset": 0,
                "limit": ConfluenceSite.SEARCH_LIMIT,
            },
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row("page_id", "title", "space_key", "url", "excerpt"),
                TablePattern.cells(
                    confluence_page.page_id, re.escape(confluence_page.title)
                ),
                r"^_rows 1-\d+; (?:end of result|more rows available, next offset=\d+)"
                r"(?:; total=\d+)?_$",
            ],
            dom=[confluence_page.page_id, confluence_page.title],
        )
        feed.call(call, expect)

    def test_fetch(self, feed: ToolFeed, confluence_page: ConfluencePage) -> None:
        call = ToolCall(
            tool="confluence_fetch",
            arguments={"page_id": confluence_page.page_id, "as_markdown": True},
        )
        result = MarkdownResult(text=confluence_page.markdown)
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
                **grep.arguments(as_markdown=True),
            },
        )
        feed.call(call, ToolExpect.of(grep.result(), dom=[confluence_page.word]))


class TestIngestTools:
    """ingest: страница уезжает в базу знаний стенда; итог — строка счётчиков."""

    def test_index_pages(self, indexed_page: ConfluencePage) -> None:
        """Сам вызов проверен фикстурой: здесь важен факт индексации."""
        if not indexed_page.page_id:
            raise AssertionError("indexed page has no id")

    def test_index_unknown_space_fails(
        self, feed: ToolFeed, confluence_site: ConfluenceSite
    ) -> None:
        """Отказ тела приходит конвертом: адрес, код и ответ сервера в тексте."""
        call = ToolCall(
            tool="confluence_index_space",
            arguments={
                "space_key": ProbeText.NO_SPACE.value,
            },
        )
        path = CflRestBuilder().space_path(ProbeText.NO_SPACE.value)
        url = confluence_site.url_of(str(path))
        failure = f"GET {url}: expected 2xx, got 404"
        expect = ToolExpect(
            mark=StepMark.FAILED,
            patterns=[
                re.escape(failure),
                re.escape(f"No space found with key : {ProbeText.NO_SPACE.value}"),
            ],
            dom=["HttpStatusError", ProbeText.NO_SPACE.value],
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
                "ocr": False,
            },
        )
        patterns: list[str] = []
        for word in confluence_attachment.words:
            patterns.append(re.escape(word))

        expect = ToolExpect(patterns=patterns, dom=list(confluence_attachment.words))
        feed.call(call, expect, timeout_sec=INGEST_TIMEOUT_SEC)


class TestKbTools:
    """kb: поиск по проиндексированной странице и пустая выдача."""

    def _columns(self) -> list[str]:
        """Шапка выдачи: те же колонки, что строит строка коллекции Confluence."""
        columns = ["distance", "format_content", "tags"]
        for meta in ConfluenceCollection.META_FIELDS:
            columns.append(meta.column)

        return columns

    def _hit_patterns(self, page: ConfluencePage) -> list[str]:
        return [
            TablePattern.row(*self._columns()),
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


class TestKbIxTools:
    """kb_*2: поиск по схеме ix базы стенда, засеянной страницами IxProbe."""

    HIT_HEADER: ClassVar[str] = TablePattern.row(
        "node_id", "surface", "url", "score", "aspect", "snippet"
    )

    def _search(self, tool: str, query: str, aspects: list[str]) -> ToolCall:
        return ToolCall(
            tool=tool,
            arguments={
                "query": query,
                "surfaces": [IxProbe.SURFACE],
                "aspects": aspects,
                **RowWindowArgs.of(limit=1),
            },
        )

    def _hit(self, node: int) -> ToolExpect:
        """Выдача поиска: шапка попаданий и первая строка — node страницы."""
        return ToolExpect(
            patterns=[self.HIT_HEADER, TablePattern.cells(str(node), IxProbe.SURFACE)],
            dom=[IxProbe.SURFACE],
        )

    def test_catalog(self, feed: ToolFeed, ix_nodes: list[int]) -> None:
        call = ToolCall(tool="kb_catalog2")
        expect = ToolExpect(
            patterns=[
                TablePattern.row("surface", "description", "nodes", "aspects"),
                TablePattern.cells(IxProbe.SURFACE),
            ],
            dom=[IxProbe.SURFACE],
        )
        feed.call(call, expect)

    def test_fts_search(self, feed: ToolFeed, ix_nodes: list[int]) -> None:
        call = self._search("kb_fts_search2", "выгрузка", ["title", "body"])
        feed.call(call, self._hit(ix_nodes[0]))

    def test_trgm_search(self, feed: ToolFeed, ix_nodes: list[int]) -> None:
        call = self._search("kb_trgm_search2", IxProbe.PAGES[0].title, ["title"])
        feed.call(call, self._hit(ix_nodes[0]))

    def test_vector_search(self, feed: ToolFeed, ix_nodes: list[int]) -> None:
        call = self._search("kb_vector_search2", "когда выгружаются заказы", ["body"])
        feed.call(call, self._hit(ix_nodes[0]))

    def test_node(self, feed: ToolFeed, ix_nodes: list[int]) -> None:
        node = ix_nodes[0]
        call = ToolCall(
            tool="kb_node2", arguments={"node_id": node, "aspects": ["title"]}
        )
        expect = ToolExpect(
            patterns=[
                f"^# {IxProbe.SURFACE} node {node}$",
                re.escape(IxProbe.PAGES[0].title),
            ],
            dom=[IxProbe.PAGES[0].title],
        )
        feed.call(call, expect)


class TestPgTools:
    """pg: соединение main стенда, своя таблица, каждый инструмент по разу."""

    def test_connection_list(self, feed: ToolFeed) -> None:
        """Общий каталог показывает postgres-строку main."""
        call = ToolCall(tool="connection_list")
        feed.call(call, ToolExpect.of(_connection_catalog(), dom=CATALOG_DOM))

    def test_query_creates_table(self, probe_table: str) -> None:
        """Сам вызов проверен фикстурой: набор команд одной транзакцией."""
        if probe_table != ProbeSql.TABLE.value:
            raise AssertionError(f"probe table is odd: {probe_table}")

    def test_query_update(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_query",
            arguments={
                "connection": "main",
                "sql": ProbeSql.UPDATE.value,
                **RowWindowArgs.of(),
            },
            code="sql",
            language="sql",
        )
        result = SqlResult(
            engine="postgres",
            statements=[SqlStatement(affected_rows=1, status="UPDATE 1")],
        )
        feed.call(call, ToolExpect.of(result, dom=["UPDATE 1"]))

    def test_query_select(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_query",
            arguments={
                "connection": "main",
                "sql": ProbeSql.SELECT.value,
                **RowWindowArgs.of(),
            },
            code="sql",
            language="sql",
        )
        rows = [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]
        result = SqlResult(
            engine="postgres",
            statements=[
                SqlStatement(
                    rows=rows, status="SELECT 2", note="rows 1-2; end of result"
                )
            ],
        )
        feed.call(call, ToolExpect.of(result, dom=["alpha", "beta"]))

    def test_list_tables(self, feed: ToolFeed, probe_table: str) -> None:
        call = ToolCall(
            tool="pg_list_tables",
            arguments={
                "connection": "main",
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
                "connection": "main",
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
        result = SqlResult(
            engine="postgres",
            statements=[SqlStatement(rows=rows, note="rows 1-3; end of result")],
        )
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


class TestChTools:
    """ch: соединение main стенда под kerberos-учёткой приложения."""

    def test_connection_list(self, feed: ToolFeed) -> None:
        """Общий каталог показывает clickhouse-строку main."""
        call = ToolCall(tool="connection_list")
        feed.call(call, ToolExpect.of(_connection_catalog(), dom=CATALOG_DOM))

    def test_connection_search(self, feed: ToolFeed) -> None:
        """Поиск по виду отбирает из каталога только clickhouse-строку."""
        call = ToolCall(
            tool="connection_search",
            arguments={"kind": "clickhouse", "name": "", "host": "", "description": ""},
        )
        rows: list[dict[str, Any]] = []
        for row in _connection_catalog().rows:
            if row["kind"] == "clickhouse":
                rows.append(dict(row))

        found = TableResult(rows=rows)
        feed.call(call, ToolExpect.of(found, dom=["main", "clickhouse"]))

    def test_query(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ch_query",
            arguments={
                "sql": ProbeSql.CH_SELECT.value,
                "connection": "main",
                **RowWindowArgs.of(),
            },
            code="sql",
            language="sql",
        )
        rows = [{"who": ProbeSql.CH_USER.value, "a": 1}]
        statement = SqlStatement(rows=rows, note="rows 1-1; end of result")
        result = SqlResult(engine="clickhouse", statements=[statement])
        feed.call(call, ToolExpect.of(result, dom=[ProbeSql.CH_USER.value]))

    def test_describe_table(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ch_describe_table",
            arguments={
                "connection": "main",
                "table": ProbeSql.CH_ONE.value,
                "database": ProbeSql.CH_SYSTEM.value,
                **RowWindowArgs.of(),
            },
        )
        address = f"{ProbeSql.CH_SYSTEM.value}.{ProbeSql.CH_ONE.value}.dummy"
        expect = ToolExpect(
            patterns=[
                TablePattern.cells("address", "database", "table", "name", "position"),
                TablePattern.cells(
                    re.escape(address),
                    ProbeSql.CH_SYSTEM.value,
                    ProbeSql.CH_ONE.value,
                    "dummy",
                    "1",
                    "UInt8",
                ),
                r"^_rows 1-1; end of result_$",
            ],
            dom=["dummy", "UInt8"],
        )
        feed.call(call, expect)

    def test_list_tables(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ch_list_tables",
            arguments={
                "connection": "main",
                "database": ProbeSql.CH_SYSTEM.value,
                **RowWindowArgs.of(limit=2),
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


class TestChEdmTools:
    """ch_edm_*: выгрузка ЕДМ UI-стенда на demo-ClickHouse, соединение edm."""

    def _call(self, tool: str, **arguments: Any) -> ToolCall:
        return ToolCall(
            tool=tool,
            arguments={
                "connection": "edm",
                "database": StandDatabase.EDM_DATABASE,
                **arguments,
                **RowWindowArgs.of(),
            },
        )

    def test_structure(self, feed: ToolFeed) -> None:
        rows: list[dict[str, Any]] = []
        for etalon_id, parent, path, table, column in (
            (
                Asset.ORDERS_AMOUNT,
                Asset.ORDERS,
                "/dwh/public/orders",
                "orders",
                "amount",
            ),
            (Asset.ORDERS_ID, Asset.ORDERS, "/dwh/public/orders", "orders", "id"),
            (Asset.V_PAID_ID, Asset.V_PAID, "/dwh/public/v_paid", "v_paid", "id"),
        ):
            rows.append(
                {
                    "etalon_id": etalon_id.value,
                    "etalon_id_parent": parent.value,
                    "path": path,
                    "table_name": table,
                    "column_name": column,
                }
            )

        statement = SqlStatement(rows=rows, note="rows 1-3; end of result")
        result = SqlResult(engine="clickhouse", statements=[statement])
        feed.call(
            self._call("ch_edm_structure"),
            ToolExpect.of(result, dom=["/dwh/public/orders", "v_paid"]),
        )

    def test_descriptions(self, feed: ToolFeed) -> None:
        row = {
            "name": "orders",
            "path": "/dwh/public/orders",
            "short_description_edm": "Orders",
            "extended_description_edm": "All orders",
            "description_from_source": "orders table",
            "ed_name": "Order",
        }
        statement = SqlStatement(rows=[row], note="rows 1-1; end of result")
        result = SqlResult(engine="clickhouse", statements=[statement])
        feed.call(
            self._call("ch_edm_descriptions", name="orders"),
            ToolExpect.of(result, dom=["All orders", "Order"]),
        )


class TestOraTools:
    """ora: соединение main стенда — первый Oracle из [ix_stand].ora_sources."""

    def test_query(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ora_query",
            arguments={
                "sql": ProbeSql.ORA_SELECT.value,
                "connection": "main",
                **RowWindowArgs.of(),
            },
            code="sql",
            language="sql",
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row("a"),
                TablePattern.row("1"),
                r"^_rows 1-1; end of result_$",
            ],
            dom=["rows 1-1; end of result"],
        )
        feed.call(call, expect)

    def test_list_tables(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ora_list_tables",
            arguments={
                "connection": "main",
                "schema_name": ProbeSql.ORA_SYSTEM.value,
                **RowWindowArgs.of(limit=2),
            },
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row("schema", "name", "kind", "status", "last_ddl_time"),
                TablePattern.cells(ProbeSql.ORA_SYSTEM.value),
                r"^_rows 1-2; more rows available, next offset=2_$",
            ],
            dom=[ProbeSql.ORA_SYSTEM.value, "next offset=2"],
        )
        feed.call(call, expect)

    def test_describe_table(self, feed: ToolFeed) -> None:
        call = ToolCall(
            tool="ora_describe_table",
            arguments={
                "connection": "main",
                "table": ProbeSql.ORA_DUAL.value,
                "schema_name": ProbeSql.ORA_SYSTEM.value,
                **RowWindowArgs.of(),
            },
        )
        address = f"{ProbeSql.ORA_SYSTEM.value}.{ProbeSql.ORA_DUAL.value}.DUMMY"
        expect = ToolExpect(
            patterns=[
                TablePattern.cells("address", "schema", "table_name", "column_name"),
                TablePattern.cells(
                    re.escape(address),
                    ProbeSql.ORA_SYSTEM.value,
                    ProbeSql.ORA_DUAL.value,
                    "DUMMY",
                    "1",
                    "VARCHAR2",
                    "1",
                ),
                r"^_rows 1-1; end of result_$",
            ],
            dom=["DUMMY", "VARCHAR2"],
        )
        feed.call(call, expect)


class TestAddressTools:
    """*_address: базовый адрес соединения без запросов к системе."""

    def _expect(self, connection: str, url: str) -> ToolExpect:
        return ToolExpect(
            patterns=[
                TablePattern.row("connection", "url"),
                TablePattern.row(connection, url),
            ],
            dom=[connection],
        )

    def test_pg_address(self, feed: ToolFeed) -> None:
        stand = Stand.required()
        url = re.escape(f"postgresql://{stand.pg_host}:") + r"\d+/\S+"
        call = ToolCall(tool="pg_address", arguments={"connection": "main"})
        feed.call(call, self._expect("main", url))

    def test_ch_address_needs_a_database(self, feed: ToolFeed) -> None:
        """У ClickHouse стенда в профиле нет базы: отказ приходит текстом тела."""
        call = ToolCall(tool="ch_address", arguments={"connection": "main"})
        expect = ToolExpect(
            mark=StepMark.FAILED,
            patterns=[re.escape("no default database in the connection")],
            dom=["AddressError"],
            log_errors=True,
        )
        feed.call(call, expect)

    def test_ora_address(self, feed: ToolFeed) -> None:
        config_path = StandApp.CHAINLIT.base_config.under(REPO_ROOT)
        oracle = StandOracle(StandLayers.compose(config_path))
        url = re.escape(f"oracle://{oracle.host}:{oracle.port}/{oracle.service}")
        call = ToolCall(tool="ora_address", arguments={"connection": "main"})
        feed.call(call, self._expect("main", url))

    def test_web_address(self, feed: ToolFeed, llm_port: int) -> None:
        root = f"{StandUrl.SCHEME.value}://{StandUrl.HOST.value}:{llm_port}"
        call = ToolCall(tool="web_address", arguments={"connection": "stand"})
        feed.call(call, self._expect("stand", re.escape(root) + "/?"))

    def test_confluence_address(
        self, feed: ToolFeed, confluence_site: ConfluenceSite
    ) -> None:
        url = str(confluence_site.profile.public_url())
        call = ToolCall(tool="confluence_address")
        expect = ToolExpect(
            patterns=[
                TablePattern.row("url"),
                TablePattern.row(re.escape(url)),
                re.escape("confluence_page: {root}/rest/api/content/<page id>"),
            ],
            dom=[url],
        )
        feed.call(call, expect)


class TestDescriberTools:
    """describe_*: описания node и edge в области треда, от записи до удаления."""

    TABLE: ClassVar[str] = "postgresql://dwh.local:5432/dwh?schema=dm&table=users"
    COLUMN: ClassVar[str] = (
        "postgresql://dwh.local:5432/dwh?schema=dm&table=users&column=id"
    )
    ID_ROW: ClassVar[str] = r"^\| (\d+) +\|"

    def _ids(self, output: str) -> list[int]:
        found: list[int] = []
        for match in re.finditer(self.ID_ROW, output, re.MULTILINE):
            found.append(int(match.group(1)))

        return found

    def test_nodes_and_edges_round_trip(self, feed: ToolFeed) -> None:
        for kind, address, text in (
            ("pg_table", self.TABLE, "users"),
            ("pg_column", self.COLUMN, "user id"),
        ):
            call = ToolCall(
                tool="describe_node",
                arguments={"kind": kind, "address": address, "description": text},
            )
            expect = ToolExpect(
                patterns=[
                    TablePattern.row("kind", "url", "action", "description"),
                    TablePattern.row(kind, re.escape(address), "inserted", text),
                ],
                dom=[text],
            )
            feed.call(call, expect)

        call = ToolCall(
            tool="describe_edge",
            arguments={
                "source": self.TABLE,
                "target": self.COLUMN,
                "kind": "similar",
                "description": "holds",
            },
        )
        expect = ToolExpect(
            patterns=[
                TablePattern.row("source", "target", "kind", "action", "description"),
                TablePattern.row(
                    re.escape(self.TABLE),
                    re.escape(self.COLUMN),
                    "similar",
                    "inserted",
                    "holds",
                ),
            ],
            dom=["holds"],
        )
        feed.call(call, expect)

        call = ToolCall(tool="describe_list_nodes", arguments=RowWindowArgs.of())
        expect = ToolExpect(
            patterns=[
                TablePattern.row("id", "kind", "url", "description"),
                r"^_rows 1-2; end of result_$",
            ],
            dom=["users", "user id"],
        )
        nodes = self._ids(feed.call(call, expect).output)

        call = ToolCall(tool="describe_list_edges", arguments=RowWindowArgs.of())
        expect = ToolExpect(
            patterns=[
                TablePattern.row("id", "source", "target", "kind", "description"),
                r"^_rows 1-1; end of result_$",
            ],
            dom=["holds"],
        )
        edges = self._ids(feed.call(call, expect).output)

        call = ToolCall(tool="describe_delete_edge", arguments={"ids": edges})
        expect = ToolExpect(
            patterns=[
                TablePattern.row("id", "action"),
                TablePattern.row(str(edges[0]), "deleted"),
            ],
            dom=["deleted"],
        )
        feed.call(call, expect)

        call = ToolCall(tool="describe_delete_node", arguments={"ids": nodes})
        patterns = [TablePattern.row("id", "action")]
        for node in nodes:
            patterns.append(TablePattern.row(str(node), "deleted"))

        feed.call(call, ToolExpect(patterns=patterns, dom=["deleted"]))


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
        result = CanvasResult(
            path=saved_diagram.path,
            label=ProbeDiagram.NAME.value,
            note=CanvasPrompt.OPENED_NOTE.value,
        )
        opened = f"opened in the canvas: {ProbeDiagram.NAME.value}"
        canvas_feed.call(call, ToolExpect.of(result, dom=[opened]))

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
            ToolExpect.of(result, dom=["outside the thread attachments dir"]),
        )

    def test_send_file(
        self, canvas_feed: ToolFeed, saved_diagram: DiagramProbe
    ) -> None:
        call = ToolCall(tool="send_file", arguments={"path": saved_diagram.path})
        result = FileResult(
            path=saved_diagram.path,
            name=ProbeDiagram.NAME.value,
            mime="application/octet-stream",
        )
        canvas_feed.call(
            call,
            ToolExpect.of(
                result, dom=[f"file attached to the chat: {ProbeDiagram.NAME.value}"]
            ),
        )


class StreamProbe(StrEnum):
    """Ход с насосами: источники трёх баз и их каналы, таблицы приёмников."""

    PG_CHANNEL = "probe"
    CH_CHANNEL = "numbers"
    ORA_CHANNEL = "levels"
    PG_SQL = "select id, name from public.ui_probe order by id"
    CH_SQL = (
        "select toInt64(number + 1) as id, toString(number + 1) as name from numbers(3)"
    )
    ORA_SQL = (
        "select cast(level as number(10)) as id, 'n' || level as name "
        "from dual connect by level <= 3"
    )
    PG_FROM_CH = "ui_stream_from_ch"
    PG_FROM_ORA = "ui_stream_from_ora"
    CH_TABLE = "from_pg"
    ORA_TABLE = "FROM_PG"
    MERGE_TREE = (
        "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
        "engine = MergeTree order by {order_by}"
    )


class TestStreamTools:
    """Модель одним ответом связывает насосы трёх баз каналами: каждый
    источник раздаётся своим приёмникам, данные ложатся во все таблицы."""

    STRATEGIES: ClassVar[Mapping[str, Any]] = {
        "schema_strategy": {"kind": "drop_and_create"},
        "delete_strategy": {"kind": "nothing"},
        "insert_strategy": {"kind": "full"},
    }
    NOT_NULL_ID: ClassVar[list[dict[str, Any]]] = [{"name": "id", "nullable": False}]

    def test_one_response_wires_every_pump(
        self,
        feed: ToolFeed,
        probe_table: str,
        stream_sinks: StreamSinks,
        stand_database: str,
    ) -> None:
        calls = [
            self._source(
                "pg_stream_out",
                StreamProbe.PG_CHANNEL,
                sql=StreamProbe.PG_SQL.value,
                wire="arrow",
                columns=self.NOT_NULL_ID,
            ),
            self._source(
                "ch_stream_out",
                StreamProbe.CH_CHANNEL,
                sql=StreamProbe.CH_SQL.value,
                wire="arrow",
            ),
            self._source(
                "ora_stream_out",
                StreamProbe.ORA_CHANNEL,
                sql=StreamProbe.ORA_SQL.value,
            ),
            self._sink(
                "pg_stream_in",
                StreamProbe.CH_CHANNEL,
                "pg from ch",
                connection="main",
                schema_name=ProbeSql.SCHEMA.value,
                table_name=StreamProbe.PG_FROM_CH.value,
            ),
            self._sink(
                "pg_stream_in",
                StreamProbe.ORA_CHANNEL,
                "pg from ora",
                connection="main",
                schema_name=ProbeSql.SCHEMA.value,
                table_name=StreamProbe.PG_FROM_ORA.value,
            ),
            self._sink(
                "ch_stream_in",
                StreamProbe.PG_CHANNEL,
                "ch from pg",
                connection=StandDatabase.SINK_CH,
                database=stream_sinks.ch_database,
                table_name=StreamProbe.CH_TABLE.value,
                tail={"order_by": "id", "create_table": StreamProbe.MERGE_TREE.value},
            ),
            self._sink(
                "ora_stream_in",
                StreamProbe.PG_CHANNEL,
                "ora from pg",
                connection=StandDatabase.SINK_ORA,
                schema_name=stream_sinks.ora_schema,
                table_name=StreamProbe.ORA_TABLE.value,
                tail={"chunk_bytes": 65536},
            ),
        ]

        grouped: list[tuple[ToolCall, ToolExpect]] = []
        for call in calls:
            grouped.append((call, ToolExpect()))

        feed.call_group(grouped)

        database = StandDatabase(StandApp.CHAINLIT, stand_database)
        numbers = [(1, "1"), (2, "2"), (3, "3")]
        levels = [(1, "n1"), (2, "n2"), (3, "n3")]
        probe = [(1, "alpha"), (2, "beta")]

        assert database.rows(StreamProbe.PG_FROM_CH.value, ("id", "name")) == numbers
        # Oracle отдаёт имена колонок заглавными, поток их не переписывает
        assert database.rows(StreamProbe.PG_FROM_ORA.value, ("ID", "NAME")) == levels
        assert (
            database.sink_ch_rows(StreamProbe.CH_TABLE.value, ("id", "name")) == probe
        )
        assert (
            database.sink_ora_rows(StreamProbe.ORA_TABLE.value, ("ID", "NAME")) == probe
        )

    @staticmethod
    def _source(tool: str, channel: StreamProbe, **arguments: Any) -> ToolCall:
        return ToolCall(
            tool=tool,
            arguments={"connection": "main", **arguments, "out": channel.value},
            code="sql",
            language="sql",
            label=f"{tool} into {channel.value}",
            hidden=("out",),
        )

    def _sink(
        self,
        tool: str,
        channel: StreamProbe,
        label: str,
        tail: Mapping[str, Any] = {},
        **arguments: Any,
    ) -> ToolCall:
        """Аргументы в порядке подписи приёмника: так их рисует лента;
        tail — параметры, объявленные после стратегий."""
        return ToolCall(
            tool=tool,
            arguments={
                **arguments,
                **self.STRATEGIES,
                **tail,
                "feed": channel.value,
            },
            label=label,
            hidden=("feed",),
        )


class TestCoverage:
    """Прогон вызвал каждый инструмент, который стенд отдаёт модели."""

    def test_every_stand_tool_is_called(self, feed: ToolFeed, llm_port: int) -> None:
        call = ToolCall(tool="connection_list")
        feed.call(call, ToolExpect.of(_connection_catalog()))

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


class TestSecondTab:
    """Карточка инструмента приходит во вторую вкладку того же треда по шине."""

    WAIT_SEC: ClassVar[float] = 15.0
    NAME: ClassVar[str] = "second-tab.mmd"
    UPLOAD_INPUT: ClassVar[str] = "#upload-button-input"

    def test_diagram_card_reaches_a_second_tab(
        self, feed: ToolFeed, open_chat: Any, sandbox_stand: StandProcess
    ) -> None:
        feed.chat.ask(ScenarioName.ANSWER.value)
        feed.chat.await_idle()
        thread_id = feed.chat.log.thread_id()
        assert thread_id

        second: ChatPage = open_chat(sandbox_stand)
        second.page.goto(
            f"{sandbox_stand.config.base_url}/thread/{thread_id}",
            wait_until="domcontentloaded",
        )
        second.page.wait_for_timeout(1000)
        second.log.clear()

        path = f"/workspace/{thread_id}/mermaid/{self.NAME}"
        call = ToolCall(
            tool="diagram_save",
            arguments={"name": self.NAME, "spec": ProbeDiagram.SPEC.value},
            code="spec",
            language="mermaid",
        )
        result = MarkdownResult(
            text=f"diagram saved: {path}; {DiagramPrompt.SAVED_NOTE.value}"
        )
        feed.call(call, ToolExpect.of(result, dom=[f"diagram saved: {path}"]))

        deadline = time.monotonic() + self.WAIT_SEC
        while not self._cards(second):
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"no card in the second tab\n{second.log.describe()}"
                )

            second.page.wait_for_timeout(100)

    def test_uploaded_file_reaches_a_second_tab(
        self,
        feed: ToolFeed,
        open_chat: Any,
        sandbox_stand: StandProcess,
        tmp_path: Path,
    ) -> None:
        """Вложение к вопросу уходит с TurnStarted: вторая вкладка треда получает
        элемент, а не только текст.
        """
        feed.chat.ask(ScenarioName.ANSWER.value)
        feed.chat.await_idle()
        thread_id = feed.chat.log.thread_id()
        assert thread_id

        second: ChatPage = open_chat(sandbox_stand)
        second.page.goto(
            f"{sandbox_stand.config.base_url}/thread/{thread_id}",
            wait_until="domcontentloaded",
        )
        second.page.wait_for_timeout(1000)
        second.log.clear()

        note = tmp_path / "tab-note.txt"
        note.write_text("hello from the first tab", encoding="utf-8")
        feed.chat.page.set_input_files(self.UPLOAD_INPUT, str(note))
        feed.chat.page.wait_for_timeout(1000)
        feed.chat.ask(ScenarioName.ANSWER.value)
        feed.chat.await_idle()

        deadline = time.monotonic() + self.WAIT_SEC
        while note.name not in self._element_names(second):
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"no attachment in the second tab\n{second.log.describe()}"
                )

            second.page.wait_for_timeout(100)

    def test_edited_question_keeps_its_attachment_for_the_model(
        self, feed: ToolFeed, llm_port: int, tmp_path: Path
    ) -> None:
        """Правка текста вопроса не отрывает от него файл: модель видит путь вложения
        и после правки.
        """
        note = tmp_path / "edit-note.txt"
        note.write_text("keep me after the edit", encoding="utf-8")
        feed.chat.page.set_input_files(self.UPLOAD_INPUT, str(note))
        feed.chat.page.wait_for_timeout(1000)
        feed.chat.ask(ScenarioName.ANSWER.value)
        feed.chat.await_idle()

        page = feed.chat.page
        page.locator(".edit-message").last.click(force=True)
        page.locator("#edit-chat-input").fill(f"{ScenarioName.ANSWER.value} edited")
        feed.chat.log.clear()
        page.locator(".confirm-edit").click()
        feed.chat.await_idle()

        response = httpx.get(
            StandUrl.of(llm_port, FakeRoute.REQUESTS.value), timeout=5.0
        )
        response.raise_for_status()
        requests = response.json()["requests"]
        assert requests, "fake llm recorded no requests"

        last_user = self._last_user_content(requests[-1])
        assert "edited" in last_user
        assert note.name in last_user

    @staticmethod
    def _last_user_content(request: Mapping[str, Any]) -> str:
        content = ""
        for message in request.get("messages") or []:
            if message.get("role") != "user":
                continue

            content = str(message.get("content"))

        return content

    @staticmethod
    def _element_names(chat: ChatPage) -> list[str]:
        names: list[str] = []
        for frame in chat.log.of_event(ChatEvent.ELEMENT):
            if not isinstance(frame.payload, dict):
                continue

            names.append(str(frame.payload.get("name")))

        return names

    @staticmethod
    def _cards(chat: ChatPage) -> list[str]:
        found: list[str] = []
        for frame in chat.log.of_event(ChatEvent.ELEMENT):
            if not isinstance(frame.payload, dict):
                continue

            if frame.payload.get("name") != CANVAS_ELEMENT:
                continue

            found.append(str(frame.payload.get("id")))

        return found
