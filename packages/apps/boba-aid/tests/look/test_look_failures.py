"""Вид сбоев: агент, служба агента и сервер MCP отказали — что видит
пользователь. Правило показа: сбой, о котором знает живой агент, — его
пометка в ленте хода (запись истории агента, та же после перезагрузки);
агент недоступен — полоса над полем ввода с причиной и кнопкой повтора,
поле ввода заперто.

Отказы разыгрывают стенды: агент процессом, который тест убивает или
который не запускается и не отвечает; служба агента (`boba serve`) и
стандартный сервер MCP за прокси с отказами (FaultProxy): соединение не
принимается, обрывается или вместо сервиса отвечает чужой HTTP; модель —
маркерами отказа фейкового провайдера. В текстах сбоев есть порты стенда и
serverId регистраций — их выравнивают правила LOCAL_PORT и SERVER_ID. Кадры
— светлая тема, окно 1280.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar

import pytest
from acp_stand import AgentHistoryStand, AppsHost
from aid_look import (
    LookAgentShape,
    LookCheck,
    LookError,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Error, Locator, Page, WebSocket, expect

from boba.cli.serve import ServeHistory
from boba.runtime.config import ProcessConfig
from boba.stand.agent_service import (
    AgentServiceStand,
    ServeAddress,
    ServeStandConfig,
    ServeThread,
    ServiceProfiles,
)
from boba.stand.faults import FaultMode, FaultProxy, FaultReply
from boba.stand.site import Stand
from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import (
    FailureName,
    Reply,
    ReplyBookClient,
    ReplyTurn,
    ScenarioName,
)
from boba.stand.ui.socket_log import ChatEvent, SocketLog
from boba.stand.ui.stand import StandPorts, StandProcess, StandUrl

pytestmark = [pytest.mark.ui, pytest.mark.look]


THEME = LookTheme.LIGHT
SIZE = LookSize.WIDE


class FailLook:
    """Состояния сбоев (FL плана тестов вида)."""

    GROUP: ClassVar[str] = "failures"
    TURN_LOST: ClassVar[LookState] = LookState(
        GROUP,
        "fl1-turn-lost",
        "Процесс агента убит посреди хода",
        "над полем ввода строка с красной точкой «The connection to the agent "
        "was lost. Reconnect with the button on the right; …», под ней мелко "
        "причина «agent "
        "process 'python…', the turn: the process exited with code -9», и "
        "кнопка «Повторить»; поле ввода заперто; в ленте — вопрос, сообщений об "
        "ошибке в ленте нет, кружка ожидания нет",
        (),
    )
    TURN_LOST_RETRIED: ClassVar[LookState] = LookState(
        GROUP,
        "fl2-turn-lost-retried",
        "Тот же тред после «Повторить»",
        "полосы нет, поле ввода открыто; лента собрана из истории агента: "
        "вопрос и под ним строка с жёлтой точкой в колонке аватара «The "
        "previous turn was cut short: the agent stopped before it finished…»",
        (),
    )
    TOOL_LOST: ClassVar[LookState] = LookState(
        GROUP,
        "fl3-tool-lost-retried",
        "Агент убит посреди вызова инструмента, тред поднят «Повторить»",
        "строка хода раскрыта: вызов sleep_for красный с текстом «[Tool call "
        "interrupted: the session ended before this call's result was "
        "recorded…]»; под ходом — пометка об оборванном ходе",
        (),
    )
    MODEL_RETRIES: ClassVar[LookState] = LookState(
        GROUP,
        "fl4-model-retries",
        "Модель трижды ответила 429, ход кончился отказом",
        "три строки с жёлтой точкой «The model is busy right now. Trying again "
        "in N seconds (attempt N of 3).», под каждой мелко «HTTP 429 Too Many "
        "Requests: fake llm: rate limit exceeded, retry later», и строка с "
        "красной точкой «The model is still busy after several tries, so this "
        "turn has stopped. …»; сырого JSON и недописанных ответов нет",
        (),
    )
    MODEL_RETRIES_RELOADED: ClassVar[LookState] = LookState(
        GROUP,
        "fl5-model-retries-reloaded",
        "Тот же тред после перезагрузки",
        "лента пиксель в пиксель совпадает с живой",
        (),
    )
    MODEL_CUT: ClassVar[LookState] = LookState(
        GROUP,
        "fl6-model-cut",
        "Поток модели обрывается на каждой попытке",
        "ни одного обрывка ответа: только строки повторов «The model's answer "
        "broke off before it was finished. Trying again …» и строка с красной "
        "точкой «The model's answer kept breaking off, so this turn has "
        "stopped. …»",
        (),
    )
    MCP_UNAUTHORIZED: ClassVar[LookState] = LookState(
        GROUP,
        "fl7-mcp-unauthorized",
        "Сервер MCP отвечает 401 страницей HTML",
        "в ходе строка с жёлтой точкой «The MCP server 'faulty' isn't "
        "available right now, so its tools can't be used in this turn. …», под "
        "ней мелко «HTTP 401 Unauthorized»; адресов и типов исключений нет; "
        "ниже ответ модели",
        (),
    )
    MCP_UNAUTHORIZED_RELOADED: ClassVar[LookState] = LookState(
        GROUP,
        "fl8-mcp-unauthorized-reloaded",
        "Тот же тред после перезагрузки",
        "пометка о сервере осталась на месте: её записал агент; лента пиксель "
        "в пиксель совпадает с живой",
        (),
    )
    MCP_HTML: ClassVar[LookState] = LookState(
        GROUP,
        "fl9-mcp-html",
        "Сервер MCP отвечает 200 страницей HTML вместо MCP",
        "строка о сервере, под ней мелко «a web page instead of an MCP reply "
        "(HTTP 200)»",
        (),
    )
    MCP_LOST_AND_BACK: ClassVar[LookState] = LookState(
        GROUP,
        "fl10-mcp-lost-and-back",
        "Сервер MCP пропал между ходами и вернулся",
        "первый ход — ответ; второй начинается строкой с жёлтой точкой «The "
        "MCP server 'faulty' isn't available right now, …» и мелкой причиной "
        "«no connection»; третий — снова ответ без пометки",
        (),
    )
    MCP_AND_MODEL: ClassVar[LookState] = LookState(
        GROUP,
        "fl11-mcp-and-model",
        "Сервер MCP отвечает 401, модель — 429",
        "видны обе причины: строка о сервере MCP, строки повторов и строка "
        "отказа модели; точки одного размера в колонке аватара, ни одна "
        "пометка не затёрла другую",
        (),
    )
    START_FAILED: ClassVar[LookState] = LookState(
        GROUP,
        "fl12-start-failed",
        "Программа агента не найдена",
        "строка с красной точкой «The agent couldn't be started, so the chat "
        "can't answer right now. Try again with the button on the right; …», "
        "мелко причина для "
        "администратора «agent process 'boba-missing', start: …» и «Повторить»; "
        "внутренних путей в тексте нет; поле заперто",
        (),
    )
    NO_ANSWER: ClassVar[LookState] = LookState(
        GROUP,
        "fl13-no-answer",
        "Агент не ответил на initialize",
        "строка с красной точкой «The agent didn't respond in time, so the chat "
        "can't answer right now. Try again in a moment with the button on the "
        "right.», мелко «agent process "
        "'python…', initialize: no answer within 8 s»; поле заперто",
        (),
    )
    SERVICE_UNREACHABLE: ClassVar[LookState] = LookState(
        GROUP,
        "fl14-service-unreachable",
        "Служба агента недоступна при открытии треда",
        "строка с красной точкой «The agent can't be reached right now, so the "
        "chat can't answer. …», мелко адрес службы и причина; лента пуста; "
        "поле заперто",
        (),
    )
    SERVICE_RETRIED: ClassVar[LookState] = LookState(
        GROUP,
        "fl15-service-retried",
        "Служба вернулась, «Повторить»",
        "полосы нет; история треда нарисована",
        (),
    )
    SERVICE_UNAUTHORIZED: ClassVar[LookState] = LookState(
        GROUP,
        "fl16-service-unauthorized",
        "Служба агента отвечает 401 страницей HTML",
        "строка «The agent didn't accept the sign-in of this chat, so the chat "
        "can't answer. Reload the page to sign in again; …»",
        (),
    )
    SERVICE_BAD_GATEWAY: ClassVar[LookState] = LookState(
        GROUP,
        "fl17-service-bad-gateway",
        "Служба агента отвечает 502 страницей HTML",
        "строка «The agent answered in a way the chat doesn't understand, …», "
        "мелко «… HTTP 502 …»",
        (),
    )
    SERVICE_LOST: ClassVar[LookState] = LookState(
        GROUP,
        "fl18-service-lost",
        "Связь со службой агента оборвалась посреди хода",
        "строка «The connection to the agent was lost. Reconnect with the button "
        "on the right; …», мелко «agent service at …, the turn: the service closed "
        "the connection»; процесса в тексте нет",
        (),
    )

    LOGIN_USER: ClassVar[str] = "admin"
    THEMED: ClassVar[str] = """(theme) => {
      window.localStorage.setItem('vite-ui-theme', theme);
      const root = document.documentElement;
      root.classList.remove('light', 'dark');
      root.classList.add(theme);
      const variables = (window.theme || {})[theme] || {};
      for (const [key, value] of Object.entries(variables)) {
        root.style.setProperty(key, value);
      }
    }"""
    """Тема страницы так же, как её ставит ThemeProvider chainlit."""
    THEME_MS: ClassVar[int] = 300
    TURN_SEC: ClassVar[float] = 120.0
    WAIT_MS: ClassVar[float] = 60_000
    BANNER: ClassVar[str] = "[data-agent-unavailable]"
    RETRY: ClassVar[str] = "#agent-retry"
    COMPOSER: ClassVar[str] = "#message-composer"
    TOOL: ClassVar[str] = "mcp__faulty__"
    TALL: ClassVar[int] = 2000
    """Высота окна кадров ленты нескольких ходов и длинных пометок."""


class FailQuestion:
    """Вопросы сценариев: маркеры сценария и отказа фейковой модели."""

    ANSWER: ClassVar[str] = f"Как дела? {ScenarioName.ANSWER.value}"
    HUNG: ClassVar[str] = "Долгий вопрос, ответ на который ещё идёт"
    """Вопрос книги ответов: поток ответа стоит на паузе, пока тест его не
    отпустит, — висящий запрос к модели не держит фейковый провайдер на
    выходе."""
    RATE: ClassVar[str] = (
        f"Вопрос под лимитом {ScenarioName.ANSWER.value} {FailureName.STATUS_429.value}"
    )
    CUT: ClassVar[str] = (
        f"Оборванный ответ {ScenarioName.ANSWER.value} {FailureName.CUT.value}"
    )

    def book(self) -> list[Reply]:
        turn = ReplyTurn(content="Ответ, который не успел", hold=True)
        return [Reply(question=self.HUNG, turns=[turn])]

    def call(self, tool: str, arguments: Mapping[str, Any]) -> str:
        spec = json.dumps({"name": f"{FailLook.TOOL}{tool}", "arguments": arguments})
        return f"{ScenarioName.CALL.value} {spec}"


class AgentPids:
    """Процессы `boba --stdio` стенда: по каталогу настроек в argv."""

    def __init__(self, stand: StandProcess) -> None:
        self._agents = str(stand.config.workdir / "agents")

    def killed(self) -> list[int]:
        """SIGKILL всем процессам агента стенда и ожидание их ухода."""
        pids = list(self._pids())
        for pid in pids:
            os.kill(pid, signal.SIGKILL)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not set(pids) & set(self._pids()):
                return pids

            time.sleep(0.1)

        msg = f"look failures: agent processes {pids} did not exit after SIGKILL"
        raise LookError(msg)

    def _pids(self) -> Iterator[int]:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue

            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue

            for part in argv:
                if part.decode(errors="replace").startswith(self._agents):
                    yield int(entry.name)
                    break


class FailTab:
    """Вкладка сценария сбоя: вопрос и конец хода по журналу сокета, полоса
    недоступного агента и её кнопка, лента от последнего вопроса и тот же
    тред, открытый заново. Ошибки страницы копятся с открытия."""

    def __init__(self, page: Page, stand: StandProcess, book: ReplyBookClient) -> None:
        self._page = page
        self._book = book
        self._log = SocketLog()
        self._chat = ChatPage(page=page, log=self._log, base_url=stand.config.base_url)
        self.errors: list[str] = []
        page.on("pageerror", self._failed)
        page.on("websocket", self._watch)

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def _watch(self, socket: WebSocket) -> None:
        socket.on("framereceived", self._log.accept)

    @property
    def page(self) -> Page:
        return self._page

    def opened(self) -> None:
        """Старт сессии chainlit после входа закончен: его задача не
        спутается с первым ходом. Страница не открывается заново — вторая
        вкладка подняла бы второго агента."""
        self._wait(lambda: self._log.task_done(ChatPage.ON_CHAT_START))

    def shots(
        self, look: LookCheck, region: Callable[[], Locator], state: LookState
    ) -> None:
        """Кадры состояния в обеих темах и обоих размерах на той же странице:
        состояние сбоя одноразовое, новая вкладка его бы не повторила. Тема
        переключается так же, как её ставит ThemeProvider chainlit, высота
        окна остаётся прежней; после кадров — снова светлая тема и ширина
        ноутбука."""
        viewport = self._page.viewport_size
        assert viewport is not None
        height = viewport["height"]
        for size in LookSize:
            self._page.set_viewport_size(
                {"width": size.viewport["width"], "height": height}
            )
            for theme in LookTheme:
                self._themed(theme)
                look.frame(region(), state, theme, size)

        self._page.set_viewport_size(
            {"width": SIZE.viewport["width"], "height": height}
        )
        self._themed(THEME)

    def _themed(self, theme: LookTheme) -> None:
        self._page.emulate_media(color_scheme=theme.value)
        self._page.evaluate(FailLook.THEMED, theme.value)
        self._page.wait_for_timeout(FailLook.THEME_MS)

    def tall(self) -> None:
        """Окно той же ширины, в которое лента нескольких ходов входит
        целиком."""
        self._page.set_viewport_size(
            {"width": SIZE.viewport["width"], "height": FailLook.TALL}
        )

    def ask(self, question: str) -> None:
        self._chat.ask(question)
        self._chat.await_idle(FailLook.TURN_SEC)

    def ask_held(self) -> None:
        """Вопрос, поток ответа на который стоит на паузе: ход идёт."""
        self._chat.ask(FailQuestion.HUNG)
        self._book.await_held(FailQuestion.HUNG, FailLook.TURN_SEC)

    def release(self) -> None:
        """Пауза потока снята: фейковый провайдер не держит запрос."""
        if FailQuestion.HUNG in self._book.held():
            self._book.release(FailQuestion.HUNG)

    def ask_running(self, question: str) -> None:
        """Вопрос, ход которого ещё идёт: тест ждёт вопрос в ленте."""
        self._chat.ask(question)
        self._chat.await_event(ChatEvent.TASK_START, FailLook.TURN_SEC)
        self._wait(lambda: self._log.last_step(StepKind.USER.value) is not None)

    def await_tool(self) -> None:
        """Ход дошёл до вызова инструмента."""
        self._wait(lambda: self._log.last_step(StepKind.TOOL.value) is not None)

    def thread_id(self) -> str:
        return self._log.thread_id()

    def banner(self) -> Locator:
        banner = self._page.locator(FailLook.BANNER)
        expect(banner).to_be_visible(timeout=FailLook.WAIT_MS)
        return banner

    def retried(self) -> None:
        """Кнопка «Повторить»: полоса уходит, лента собрана заново."""
        self._page.locator(FailLook.RETRY).click()
        expect(self._page.locator(FailLook.BANNER)).to_have_count(
            0, timeout=FailLook.WAIT_MS
        )

    def composer(self) -> Locator:
        return self._page.locator(FailLook.COMPOSER)

    def feed(self) -> Locator:
        """Лента, прокрученная к последнему вопросу."""
        asked = self._page.locator(Selector.of_type(StepKind.USER.value)).last
        asked.wait_for(timeout=FailLook.WAIT_MS)
        asked.evaluate(LookPages.TOP)
        return asked.locator(LookPages.FEED)

    def whole(self) -> Locator:
        """Лента всех ходов, прокрученная к первому вопросу."""
        asked = self._page.locator(Selector.of_type(StepKind.USER.value)).first
        asked.wait_for(timeout=FailLook.WAIT_MS)
        asked.evaluate(LookPages.TOP)
        return asked.locator(LookPages.FEED)

    def shown(self, text: str) -> None:
        expect(self._page.get_by_text(text).last).to_be_visible(
            timeout=FailLook.WAIT_MS
        )

    def open_steps(self) -> None:
        """Раскрывает строку хода и его шаги."""
        for kind in (StepKind.RUN, StepKind.TOOL):
            step = self._page.locator(Selector.of_type(kind.value)).last
            toggle = step.locator(Selector.STEP_TOGGLE.value).first
            toggle.wait_for(timeout=FailLook.WAIT_MS)
            if toggle.get_attribute(Selector.TOGGLE_STATE.value) != Selector.OPENED:
                toggle.click()

    def reload(self) -> None:
        """Тот же тред, открытый заново: лента — из истории агента."""
        thread_id = self.thread_id()
        self._log.clear()
        self._page.goto(
            f"{self._chat.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        self._chat.await_event(ChatEvent.HISTORY_LOADED, FailLook.TURN_SEC)
        self._page.wait_for_load_state("networkidle")

    def check_clean(self) -> None:
        if self.errors:
            raise LookError(f"the chat page raised errors: {self.errors}")

    def _wait(self, done: Callable[[], bool]) -> None:
        deadline = time.monotonic() + FailLook.TURN_SEC
        while not done():
            if time.monotonic() > deadline:
                msg = (
                    f"look failures: the tab did not get there\n{self._log.describe()}"
                )
                raise LookError(msg)

            self._page.wait_for_timeout(100)


class FailTabs:
    """Вкладки сценариев: свой вход на каждую; книга ответов фейкового
    провайдера кладётся заново."""

    def __init__(self, pages: LookPages, book: ReplyBookClient) -> None:
        self._pages = pages
        self._book = book
        book.register(FailQuestion().book())

    def open(self, stand: StandProcess) -> FailTab:
        """Вкладка после входа и старта сессии chainlit: конец её задачи
        старта не путается с концом первого хода."""
        page = self._pages.open(THEME, SIZE)
        tab = FailTab(page, stand, self._book)
        self._pages.sign_in(page, stand, FailLook.LOGIN_USER)
        tab.opened()
        return tab

    def settle(self, tab: FailTab) -> None:
        self._pages.settle(tab.page)


class FaultyServer:
    """Стандартный сервер MCP за прокси с отказами: секция [mcp.servers]
    чата смотрит на прокси."""

    NAME: ClassVar[str] = "faulty"

    def __init__(self, host: AppsHost) -> None:
        section = host.plain()
        self._section = section
        self.proxy = FaultProxy(int(section["endpoint"]["service"]["port"]))

    def section(self) -> dict[str, Any]:
        section = json.loads(json.dumps(self._section))
        section["endpoint"]["service"]["port"] = self.proxy.port
        section["limits"] = {"connect_timeout_sec": 5.0, "call_timeout_sec": 60.0}
        return section

    def reply(self, status: int) -> None:
        page = FaultReply(
            status=HTTPStatus(status),
            content_type="text/html",
            body=FaultReply.HTML_PAGE,
        )
        self.proxy.set(FaultMode.REPLY, page)

    def through(self) -> None:
        self.proxy.set(FaultMode.THROUGH)


class ServiceStand:
    """Служба агента за прокси с отказами и стенд чата, чей агент — эта
    служба: [agent.process] называет адрес прокси и вход proxy."""

    PROFILE: ClassVar[str] = "general"
    MODEL: ClassVar[str] = "fake-model-general"
    ROLES: ClassVar[tuple[str, ...]] = ("ADM", "DEV")

    def __init__(
        self, process_config: ProcessConfig, stand: Stand, llm_port: int
    ) -> None:
        listen = StandPorts().free()
        self.proxy = FaultProxy(listen)
        self._address = ServeAddress(listen=listen, public=self.proxy.port)
        self._krb = process_config.krb
        history = AgentHistoryStand(stand)
        self._history = ServeHistory(
            schema=history.schema(), postgres=history.postgres()
        )
        self._profiles = ServiceProfiles(StandUrl.of(llm_port, "/v1")).add(
            self.PROFILE, list(self.ROLES), self.MODEL
        )
        self.serve = ServeThread(self._served)

    def _served(self) -> AgentServiceStand:
        settings = {
            "permissions": {"defaultMode": "bypassPermissions", "allowBypass": True},
            "sessions": {"maxOpen": 8},
        }
        return AgentServiceStand(
            self._krb, self._history, settings, self._profiles, 600.0, self._address
        )

    def agent(self) -> dict[str, Any]:
        """Секция [agent.process] чата: служба за прокси, вход proxy."""
        location = {
            "scheme": StandUrl.SCHEME.value,
            "host": StandUrl.HOST.value,
            "port": self.proxy.port,
            "path": "",
        }
        return {
            "endpoint": {"service": location, "path": ServeStandConfig.PATH},
            "auth": {
                "auth": "proxy",
                "issuer": location,
                "client_id": ServeStandConfig.CLIENT_ID,
                "client_secret": ServeStandConfig.CLIENT_SECRET,
                "secret": ServeStandConfig.PROXY_SECRET,
            },
        }

    def reply(self, status: HTTPStatus) -> None:
        page = FaultReply(
            status=status, content_type="text/html", body=FaultReply.HTML_PAGE
        )
        self.proxy.set(FaultMode.REPLY, page)

    def stop(self) -> None:
        self.serve.stop()
        self.proxy.stop()


@pytest.fixture(scope="module")
def faulty(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FaultyServer]:
    host = AppsHost(tmp_path_factory.mktemp("look-failures-apps"))
    server = FaultyServer(host)
    try:
        host.await_listening()
        yield server
    finally:
        server.proxy.stop()
        host.stop()


@pytest.fixture(scope="module")
def process_stand(look_stands: LookStand, faulty: FaultyServer) -> StandProcess:
    shape = LookAgentShape(servers={FaultyServer.NAME: faulty.section()})
    return look_stands.shaped("failures-process", shape)


@pytest.fixture(scope="module")
def service(
    process_config: ProcessConfig, stand: Stand, look_llm_port: int
) -> Iterator[ServiceStand]:
    served = ServiceStand(process_config, stand, look_llm_port)
    served.serve.start()
    try:
        yield served
    finally:
        served.stop()


@pytest.fixture(scope="module")
def service_stand(look_stands: LookStand, service: ServiceStand) -> StandProcess:
    shape = LookAgentShape(service=service.agent())
    return look_stands.shaped("failures-service", shape)


@pytest.fixture
def tabs(look_pages: LookPages, look_llm_port: int) -> FailTabs:
    return FailTabs(look_pages, ReplyBookClient(StandUrl.of(look_llm_port)))


def test_lost_agent_look(
    look: LookCheck, tabs: FailTabs, process_stand: StandProcess
) -> None:
    """Агент убит посреди хода и посреди вызова: полоса с причиной, после
    «Повторить» — лента из истории с пометкой об оборванном ходе."""
    questions = FailQuestion()
    agents = AgentPids(process_stand)
    tab = tabs.open(process_stand)
    tab.ask(FailQuestion.ANSWER)
    tab.ask_held()
    agents.killed()
    tab.release()
    tab.banner()
    tabs.settle(tab)
    tab.shots(look, tab.composer, FailLook.TURN_LOST)

    tab.retried()
    tab.shown("The previous turn was cut short")
    tabs.settle(tab)
    tab.shots(look, tab.feed, FailLook.TURN_LOST_RETRIED)

    tab.ask_running(questions.call("sleep_for", {"seconds": 60}))
    tab.await_tool()
    agents.killed()
    tab.banner()
    tab.retried()
    tab.shown("The previous turn was cut short")
    tab.open_steps()
    tab.shown("Tool call interrupted")
    tabs.settle(tab)
    tab.shots(look, tab.feed, FailLook.TOOL_LOST)
    tab.check_clean()
    look.verify()


def test_model_failure_look(
    look: LookCheck, tabs: FailTabs, process_stand: StandProcess
) -> None:
    """Отказы модели — пометки агента: повторы и отказ видны вживую и
    те же после перезагрузки; обрывки неудачных попыток не остаются."""
    tab = tabs.open(process_stand)
    tab.tall()
    tab.ask(FailQuestion.RATE)
    tab.shown("(attempt 3 of 3)")
    tabs.settle(tab)
    tab.shots(look, tab.feed, FailLook.MODEL_RETRIES)
    tab.reload()
    tab.shown("(attempt 3 of 3)")
    tabs.settle(tab)
    tab.shots(look, tab.feed, FailLook.MODEL_RETRIES_RELOADED)
    look.same_as(FailLook.MODEL_RETRIES_RELOADED, FailLook.MODEL_RETRIES, THEME, SIZE)

    tab.ask(FailQuestion.CUT)
    tab.shown("kept breaking off")
    expect(tab.page.get_by_text("Here is a plain streamed answer")).to_have_count(0)
    tabs.settle(tab)
    tab.shots(look, tab.feed, FailLook.MODEL_CUT)
    tab.check_clean()
    look.verify()


def test_mcp_failure_look(
    look: LookCheck,
    tabs: FailTabs,
    process_stand: StandProcess,
    faulty: FaultyServer,
) -> None:
    """Отказы сервера MCP — пометки агента в ходе с кодом, видом тела или
    причиной соединения; пометка — запись истории. Сессия агента
    открывается на каждый ход, поэтому сервер, пропавший между ходами,
    назван пометкой следующего хода, а вернувшийся молча снова в сессии."""
    try:
        faulty.reply(HTTPStatus.UNAUTHORIZED)
        tab = tabs.open(process_stand)
        tab.ask(FailQuestion.ANSWER)
        tab.shown("401 Unauthorized")
        tabs.settle(tab)
        tab.shots(look, tab.feed, FailLook.MCP_UNAUTHORIZED)
        tab.reload()
        tab.shown("401 Unauthorized")
        tabs.settle(tab)
        tab.shots(look, tab.feed, FailLook.MCP_UNAUTHORIZED_RELOADED)
        look.same_as(
            FailLook.MCP_UNAUTHORIZED_RELOADED, FailLook.MCP_UNAUTHORIZED, THEME, SIZE
        )

        faulty.reply(HTTPStatus.OK)
        tab = tabs.open(process_stand)
        tab.ask(FailQuestion.ANSWER)
        tab.shown("a web page instead of an MCP reply (HTTP 200)")
        tabs.settle(tab)
        tab.shots(look, tab.feed, FailLook.MCP_HTML)

        faulty.through()
        tab = tabs.open(process_stand)
        tab.tall()
        tab.ask(FailQuestion.ANSWER)
        faulty.proxy.set(FaultMode.REFUSE)
        tab.ask(FailQuestion.ANSWER)
        tab.shown("no connection")
        faulty.through()
        tab.ask(FailQuestion.ANSWER)
        tabs.settle(tab)
        tab.shots(look, tab.whole, FailLook.MCP_LOST_AND_BACK)

        faulty.reply(HTTPStatus.UNAUTHORIZED)
        tab = tabs.open(process_stand)
        tab.tall()
        tab.ask(FailQuestion.RATE)
        tab.shown("(attempt 3 of 3)")
        tab.shown("401 Unauthorized")
        tabs.settle(tab)
        tab.shots(look, tab.feed, FailLook.MCP_AND_MODEL)
        tab.check_clean()
    finally:
        faulty.through()

    look.verify()


def test_agent_start_look(
    look: LookCheck, tabs: FailTabs, look_stands: LookStand
) -> None:
    """Агент не запустился или молчит: полоса с причиной сразу на открытой
    вкладке, без вопроса."""
    missing = LookAgentShape(
        process={"command": "/nonexistent/boba-missing", "args": ["--stdio"]}
    )
    tab = tabs.open(look_stands.shaped("failures-missing", missing))
    tab.banner()
    tabs.settle(tab)
    tab.shots(look, tab.composer, FailLook.START_FAILED)

    silent = LookAgentShape(
        process={
            "command": f"{sys.executable} -c 'import time; time.sleep(600)'",
            "args": [],
        },
        timeouts_sec=(8.0, 8.0),
    )
    tab = tabs.open(look_stands.shaped("failures-silent", silent))
    tab.banner()
    tabs.settle(tab)
    tab.shots(look, tab.composer, FailLook.NO_ANSWER)
    tab.check_clean()
    look.verify()


def test_service_failure_look(
    look: LookCheck,
    tabs: FailTabs,
    service: ServiceStand,
    service_stand: StandProcess,
) -> None:
    """Служба агента недоступна, отказала во входе, ответила чужим HTTP,
    оборвала связь посреди хода: полоса с причиной; «Повторить» после
    возврата службы рисует историю."""
    try:
        tab = tabs.open(service_stand)
        tab.ask(FailQuestion.ANSWER)
        service.proxy.set(FaultMode.REFUSE)
        tab.reload()
        tab.banner()
        tabs.settle(tab)
        tab.shots(look, tab.composer, FailLook.SERVICE_UNREACHABLE)
        service.proxy.set(FaultMode.THROUGH)
        tab.retried()
        tab.shown("Here is a plain streamed answer")
        tabs.settle(tab)
        tab.shots(look, tab.feed, FailLook.SERVICE_RETRIED)

        service.reply(HTTPStatus.UNAUTHORIZED)
        tab = tabs.open(service_stand)
        tab.banner()
        tabs.settle(tab)
        tab.shots(look, tab.composer, FailLook.SERVICE_UNAUTHORIZED)

        service.reply(HTTPStatus.BAD_GATEWAY)
        tab = tabs.open(service_stand)
        tab.banner()
        tabs.settle(tab)
        tab.shots(look, tab.composer, FailLook.SERVICE_BAD_GATEWAY)

        service.proxy.set(FaultMode.THROUGH)
        tab = tabs.open(service_stand)
        tab.ask(FailQuestion.ANSWER)
        tab.ask_held()
        service.proxy.set(FaultMode.DROP)
        tab.banner()
        tab.release()
        tabs.settle(tab)
        tab.shots(look, tab.composer, FailLook.SERVICE_LOST)
        tab.check_clean()
    finally:
        service.proxy.set(FaultMode.THROUGH)

    look.verify()
