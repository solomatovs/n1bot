"""Вход человека в сервис целиком: настоящий процесс boba-mcp, штатный клиент
OAuth (тот же путь, что у Claude Code и IDE) и настоящий браузер.

Клиент MCP работает в своём потоке: сам находит сервер авторизации,
регистрируется, просит открыть браузер и слушает возврат на localhost.
Браузером управляет тест: открывает выданный адрес, читает DOM страницы
входа, вводит логин и пароль. Сервис между шагами перезапускается: состояния
входа у него нет, и рестарт не должен рвать ни начатый вход, ни регистрацию
клиента.

pytest -m ui.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import queue
import secrets
import threading
import time
import tomllib
import webbrowser
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.auth import OAuth
from fastmcp.client.transports import StreamableHttpTransport
from key_value.aio.stores.memory import MemoryStore
from omegaconf import DictConfig, OmegaConf
from playwright.sync_api import Browser, Page, expect

from boba.stand.ui.stand import (
    REPO_ROOT,
    ServiceStand,
    StandApp,
    StandProcess,
    free_port,
)

pytestmark = pytest.mark.ui

BOOT_SEC = 180.0
STEP_SEC = 60.0
ENDPOINT = "general"
TOOL = "canvas_open"
"""Инструмент, который есть у endpoint'а и стенда, и развёрнутого сервиса."""
LOGIN_FIELD = 'input[name="login"]'
PASSWORD_FIELD = 'input[name="password"]'
SUBMIT = 'form button[type="submit"]'
ALERT = '[role="alert"]'
SSO_LINK = 'a[href*="/login/sso"]'


class SignInSession:
    """Одна сессия клиента MCP со штатным входом OAuth, в своём потоке.

    Создаётся тестом на каждое подключение клиента. Хранилище storage —
    память клиента между подключениями: регистрация и токены, как у Claude
    Code между запусками. Список инструментов сервиса — итог подключения:
    он означает, что вход пройден и сервис клиента принял.
    """

    def __init__(self, url: str, storage: MemoryStore, callback_port: int) -> None:
        self._url = url
        self._storage = storage
        self._callback_port = callback_port
        self._tools: list[str] = []
        self._failure: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def tools(self) -> Sequence[str]:
        """Инструменты, которые сервис отдал клиенту; сбой клиента — он же."""
        self._thread.join(STEP_SEC)
        if self._thread.is_alive():
            raise AssertionError(f"the mcp client did not finish in {STEP_SEC}s")

        if self._failure:
            raise AssertionError(f"the mcp client failed: {self._failure[0]!r}")

        return tuple(self._tools)

    def _run(self) -> None:
        try:
            asyncio.run(self._connected())
        except BaseException as exc:
            self._failure.append(exc)

    async def _connected(self) -> None:
        auth = OAuth(
            mcp_url=self._url,
            token_storage=self._storage,
            callback_port=self._callback_port,
            callback_timeout=STEP_SEC,
            client_name="boba sign-in ui test",
        )
        async with Client(StreamableHttpTransport(self._url, auth=auth)) as client:
            for tool in await client.list_tools():
                self._tools.append(tool.name)


class OpenedLinks:
    """Адреса, которые клиент просил открыть в браузере пользователя.

    Создаётся фикстурой opened: она подменяет открытие системного браузера,
    и адрес авторизации приходит тесту — его открывает playwright.
    """

    def __init__(self) -> None:
        self._links: queue.Queue[str] = queue.Queue()

    def open(self, url: str) -> bool:
        self._links.put(url)

        return True

    def next(self) -> str:
        """Очередной адрес авторизации; клиент его не попросил — отказ."""
        try:
            return self._links.get(timeout=STEP_SEC)
        except queue.Empty as exc:
            msg = f"the mcp client asked for no browser in {STEP_SEC}s"
            raise AssertionError(msg) from exc

    def none_asked(self) -> bool:
        return self._links.empty()


class CallbackListener:
    """Адрес возврата клиента OAuth на localhost: принимает переход браузера
    с кодом авторизации. Создаётся фикстурой callback для теста, который
    ведёт вход сам, без штатного клиента."""

    def __init__(self) -> None:
        self._returned: queue.Queue[str] = queue.Queue()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def address(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/callback"

    def returned(self) -> str:
        """Адрес, на который вернулся браузер, с кодом и состоянием."""
        try:
            return self._returned.get(timeout=STEP_SEC)
        except queue.Empty as exc:
            msg = f"the browser did not return to the client in {STEP_SEC}s"
            raise AssertionError(msg) from exc

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        returned = self._returned

        class Returned(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                returned.put(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"returned")

            def log_message(
                self,
                format: str,  # noqa: A002 — имя задаёт базовый класс
                *args: object,
            ) -> None:
                return

        return Returned


class LoginPage:
    """Страница входа сервиса в браузере: то, что видит и делает человек."""

    def __init__(self, page: Page) -> None:
        self._page = page

    def open(self, url: str) -> None:
        self._page.goto(url)
        expect(self._page.locator("h1")).to_have_text("Sign in to boba-mcp")

    def shows_the_form(self) -> None:
        expect(self._page.locator(LOGIN_FIELD)).to_be_visible()
        expect(self._page.locator(PASSWORD_FIELD)).to_be_visible()
        expect(self._page.locator(SUBMIT)).to_be_visible()

    def sign_in(self, login: str, password: str) -> None:
        self._page.fill(LOGIN_FIELD, login)
        self._page.fill(PASSWORD_FIELD, password)
        self._page.click(SUBMIT)

    def returned_to_the_client(self) -> None:
        """Браузер ушёл на адрес возврата клиента на localhost."""
        self._page.wait_for_url("**/callback?**", timeout=STEP_SEC * 1000)

    def alert(self) -> str:
        return self._page.locator(ALERT).inner_text()

    def dom(self) -> str:
        return self._page.content()


def _service(
    workdir: Path, raw: DictConfig, generation: str, ttl_sec: int
) -> Iterator[StandProcess[ServiceStand]]:
    db_name = OmegaConf.select(raw, "postgres.dbname")
    if not isinstance(db_name, str):
        msg = (
            "sign-in stand: section [postgres] of the service config expects "
            f"dbname as a string, got {db_name!r}"
        )
        raise TypeError(msg)

    config = ServiceStand(
        workdir=workdir,
        app_port=free_port(),
        db_name=db_name,
        sandbox=False,
        generation=generation,
        session_ttl_sec=ttl_sec,
    )
    process = StandProcess[ServiceStand](config=config, log_path=workdir / "mcp.log")
    process.start(BOOT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def service(
    tmp_path_factory: pytest.TempPathFactory, service_raw_config: DictConfig
) -> Iterator[StandProcess[ServiceStand]]:
    """Сервис со случайным поколением сессий: рестарт снимает все входы."""
    yield from _service(tmp_path_factory.mktemp("sign-in"), service_raw_config, "", 300)


@pytest.fixture(scope="module")
def lasting(
    tmp_path_factory: pytest.TempPathFactory, service_raw_config: DictConfig
) -> Iterator[StandProcess[ServiceStand]]:
    """Сервис с заданным поколением сессий и коротким токеном доступа: вход
    переживает рестарт, а токен приходится продлевать."""
    yield from _service(
        tmp_path_factory.mktemp("sign-in-lasting"), service_raw_config, "stand", 4
    )


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> OpenedLinks:
    links = OpenedLinks()
    monkeypatch.setattr(webbrowser, "open", links.open)

    return links


@pytest.fixture
def callback() -> Iterator[CallbackListener]:
    listener = CallbackListener()
    listener.start()
    try:
        yield listener
    finally:
        listener.stop()


@pytest.fixture
def login_page(browser: Browser) -> Iterator[LoginPage]:
    context = browser.new_context()
    try:
        yield LoginPage(context.new_page())
    finally:
        context.close()


def _public(process: StandProcess[ServiceStand]) -> str:
    return f"http://127.0.0.1:{process.config.app_port}"


def _endpoint(process: StandProcess[ServiceStand]) -> str:
    return f"{_public(process)}/mcp/{ENDPOINT}"


def _restart(process: StandProcess[ServiceStand]) -> None:
    process.stop()
    process.start(BOOT_SEC)


def _signed_in(
    process: StandProcess[ServiceStand],
    storage: MemoryStore,
    opened: OpenedLinks,
    login_page: LoginPage,
) -> Sequence[str]:
    """Подключение клиента с входом человека формой; итог — инструменты."""
    session = SignInSession(_endpoint(process), storage, free_port())
    session.start()
    login_page.open(opened.next())
    login_page.shows_the_form()
    login_page.sign_in(ServiceStand.LOCAL_LOGIN, ServiceStand.LOCAL_PASSWORD)
    login_page.returned_to_the_client()

    return session.tools()


def _connected(
    process: StandProcess[ServiceStand], storage: MemoryStore
) -> Sequence[str]:
    """Подключение клиента, которому браузер не понадобится."""
    session = SignInSession(_endpoint(process), storage, free_port())
    session.start()

    return session.tools()


class TestBrowserSignIn:
    """Человек подключает сервис к клиенту MCP и входит формой в браузере."""

    def test_sign_in_reaches_the_tools(
        self,
        service: StandProcess[ServiceStand],
        opened: OpenedLinks,
        login_page: LoginPage,
    ) -> None:
        session = SignInSession(_endpoint(service), MemoryStore(), free_port())
        session.start()

        address = opened.next()
        if not address.startswith(f"{_public(service)}/authorize?"):
            raise AssertionError(f"the client opens the service authorize: {address}")

        login_page.open(address)
        login_page.shows_the_form()
        dom = login_page.dom()
        if 'name="txn"' not in dom:
            raise AssertionError(f"the form carries the started sign-in: {dom}")

        if "/login/sso" in dom:
            raise AssertionError(f"no kerberos link without kerberos in [auth]: {dom}")

        login_page.sign_in(ServiceStand.LOCAL_LOGIN, ServiceStand.LOCAL_PASSWORD)
        login_page.returned_to_the_client()

        tools = session.tools()
        if TOOL not in tools:
            raise AssertionError(f"the signed-in client gets the tools: {tools}")

    def test_wrong_password_keeps_the_form(
        self,
        service: StandProcess[ServiceStand],
        opened: OpenedLinks,
        login_page: LoginPage,
    ) -> None:
        session = SignInSession(_endpoint(service), MemoryStore(), free_port())
        session.start()
        login_page.open(opened.next())

        login_page.sign_in(ServiceStand.LOCAL_LOGIN, "not-the-password")

        if "Invalid username or password" not in login_page.alert():
            raise AssertionError(f"the page names the refusal: {login_page.dom()}")

        login_page.shows_the_form()
        login_page.sign_in(ServiceStand.LOCAL_LOGIN, ServiceStand.LOCAL_PASSWORD)
        login_page.returned_to_the_client()
        if TOOL not in session.tools():
            raise AssertionError("the same page signs in with the right password")

    def test_made_up_link_is_refused(
        self, service: StandProcess[ServiceStand], login_page: LoginPage
    ) -> None:
        login_page.open(f"{_public(service)}/login?txn=not-started")

        if "not started or has expired" not in login_page.alert():
            raise AssertionError(f"the page names the refusal: {login_page.dom()}")

        if 'name="password"' in login_page.dom():
            raise AssertionError("a sign-in that was not started offers no form")


class TestRestarts:
    """Сервис перезапускается посреди входа: состояния входа у него нет."""

    def test_client_is_known_after_a_restart(
        self,
        service: StandProcess[ServiceStand],
        opened: OpenedLinks,
        login_page: LoginPage,
    ) -> None:
        """Рестарт со случайным поколением снимает вход, но не регистрацию
        клиента: клиент со старым client_id снова ведёт на страницу входа."""
        storage = MemoryStore()
        _signed_in(service, storage, opened, login_page)

        _restart(service)

        session = SignInSession(_endpoint(service), storage, free_port())
        session.start()
        address = opened.next()
        login_page.open(address)
        login_page.shows_the_form()
        login_page.sign_in(ServiceStand.LOCAL_LOGIN, ServiceStand.LOCAL_PASSWORD)
        login_page.returned_to_the_client()
        if TOOL not in session.tools():
            raise AssertionError("the remembered client signs in after a restart")

    def test_open_page_signs_in_after_a_restart(
        self,
        service: StandProcess[ServiceStand],
        opened: OpenedLinks,
        login_page: LoginPage,
    ) -> None:
        session = SignInSession(_endpoint(service), MemoryStore(), free_port())
        session.start()
        login_page.open(opened.next())
        login_page.shows_the_form()

        _restart(service)

        login_page.sign_in(ServiceStand.LOCAL_LOGIN, ServiceStand.LOCAL_PASSWORD)
        login_page.returned_to_the_client()
        if TOOL not in session.tools():
            raise AssertionError("the page opened before the restart signs in")

    def test_code_is_exchanged_after_a_restart(
        self,
        service: StandProcess[ServiceStand],
        browser: Browser,
        callback: CallbackListener,
    ) -> None:
        """Код авторизации выдан до рестарта, обмен на токен — после."""
        public = _public(service)
        redirect = callback.address()
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        with httpx.Client() as http:
            registered = http.post(
                f"{public}/register",
                json={
                    "redirect_uris": [redirect],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            client_id = registered.json()["client_id"]
            query = httpx.QueryParams(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "stand-state",
                    "resource": _endpoint(service),
                }
            )
            context = browser.new_context()
            try:
                login = LoginPage(context.new_page())
                login.open(f"{public}/authorize?{query}")
                login.sign_in(ServiceStand.LOCAL_LOGIN, ServiceStand.LOCAL_PASSWORD)
                login.returned_to_the_client()
            finally:
                context.close()

            _restart(service)

            back = httpx.URL(callback.returned())
            issued = http.post(
                f"{public}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": back.params["code"],
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "code_verifier": verifier,
                    "resource": _endpoint(service),
                },
            )

        if issued.status_code != httpx.codes.OK:
            raise AssertionError(f"the code survives the restart: {issued.text}")

        if "refresh_token" not in issued.json():
            raise AssertionError(f"a person gets a refresh token: {issued.json()}")


class TestLastingSession:
    """Заданное поколение сессий: вход переживает рестарт, токен доступа
    продлевается без браузера."""

    def test_session_survives_a_restart(
        self,
        lasting: StandProcess[ServiceStand],
        opened: OpenedLinks,
        login_page: LoginPage,
    ) -> None:
        storage = MemoryStore()
        _signed_in(lasting, storage, opened, login_page)

        _restart(lasting)

        tools = _connected(lasting, storage)
        if TOOL not in tools:
            raise AssertionError(f"the kept sign-in reaches the tools: {tools}")

        if not opened.none_asked():
            raise AssertionError("a kept sign-in asks for no browser")

    def test_expired_token_is_renewed_without_the_browser(
        self,
        lasting: StandProcess[ServiceStand],
        opened: OpenedLinks,
        login_page: LoginPage,
    ) -> None:
        storage = MemoryStore()
        _signed_in(lasting, storage, opened, login_page)

        time.sleep(lasting.config.session_ttl_sec + 2)

        tools = _connected(lasting, storage)
        if TOOL not in tools:
            raise AssertionError(f"the renewed sign-in reaches the tools: {tools}")

        if not opened.none_asked():
            raise AssertionError("the renewal asks for no browser")


class TestDeployedService:
    """Тот же вход против развёрнутого сервиса через nginx: публичный адрес
    с префиксом, HTTPS, метаданные от корня хоста."""

    SITE: Path = REPO_ROOT / "compose" / "mcp" / "conf" / "site.toml"
    """Специфика развёрнутого сервиса: тест ходит к контейнеру compose."""

    def test_sign_in_through_nginx(
        self, opened: OpenedLinks, login_page: LoginPage
    ) -> None:
        with self.SITE.open("rb") as handle:
            deployed = tomllib.load(handle)

        public = str(deployed["env"]["public_url"]).rstrip("/")
        try:
            health = httpx.get(f"{public}/health", timeout=10.0)
        except httpx.HTTPError as exc:
            pytest.skip(f"the deployed service at {public} is not reachable: {exc}")

        if health.status_code != httpx.codes.OK:
            pytest.skip(
                f"the deployed service at {public} answers {health.status_code}"
            )

        users = deployed["site"]["local_auth"]["users"]
        roles = deployed["site"]["local_auth"]["roles"]
        login = ""
        for name in users:
            if roles.get(name):
                login = name
                break

        if not login:
            pytest.skip("the deployed config has no local user with roles")

        with StandApp.MCP.files().config.open("rb") as handle:
            common = tomllib.load(handle)

        url = f"{public}{common['mcp']['path']}/{ENDPOINT}"
        session = SignInSession(url, MemoryStore(), free_port())
        session.start()
        address = opened.next()
        if not address.startswith(f"{public}/authorize?"):
            raise AssertionError(f"the client opens the public authorize: {address}")

        login_page.open(address)
        login_page.shows_the_form()
        if SSO_LINK and "/login/sso" not in login_page.dom():
            raise AssertionError("the deployed page links the kerberos sign-in")

        login_page.sign_in(login, str(users[login]))
        login_page.returned_to_the_client()

        tools = session.tools()
        if TOOL not in tools:
            raise AssertionError(f"the deployed service gives the tools: {tools}")
