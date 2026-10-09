"""Вход через SPNEGO: доходит ли до приложения делегированный тикет пользователя.

Сначала проверяется сервер клиентом, который Negotiate умеет заведомо, затем —
браузером. Браузер здесь настоящий: chromium берёт TGT из ccache и проходит
обмен сам. К стенду он ходит через DomainProxy по доменному имени: SPN запроса
браузер собирает из имени в адресе, и оно обязано совпасть с тем, на которое
выдан keytab приложения.

Браузерной части нужна полная сборка chromium: headless-shell собран без
сетевой аутентификации. Браузер без Negotiate — отказ теста, а не пропуск.
"""

from __future__ import annotations

import base64
import http.server
import json
import selectors
import socket
import socketserver
import threading
from collections.abc import Iterator
from enum import IntEnum
from typing import ClassVar

import httpx
import krb5
import pytest
from chat_ui import BOOT_TIMEOUT_SEC
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect
from playwright.sync_api import Error as PlaywrightError

from boba.identity.token import CookieJar, SessionClaims
from boba.kerberos import KerberosPasswordAuth
from boba.stand.site import Stand
from boba.stand.ui.database import run_blocking
from boba.stand.ui.stand import (
    StandApp,
    StandAuth,
    StandConfig,
    StandProcess,
    StandUrl,
    free_port,
)
from boba.transport.http import (
    HttpRequest,
    HttpStatusError,
    HttpTransport,
    HttpTransportConfig,
)
from boba.transport.http.connection import HttpConnection, NegotiateAuth, UrlScheme

pytestmark = pytest.mark.ui

STAND = Stand.required()

SSO_BUTTON = "#sso-login-btn"
CHAT_INPUT = "#chat-input"
SESSION_COOKIE = "access_token"

CAPTURED = "captured delegated credentials"
"""Строка лога успешного захвата: по ней видно, что делегирование доехало."""

NO_DELEGATION = "no delegated_credentials"
"""Строка лога, когда accept прошёл, а evidence-кредов KDC не дал."""

REJECTED = "delegated credentials of"
"""Строка лога, когда креды пришли, но не подошли режиму делегирования."""


class SocksByte(IntEnum):
    """Байты запроса SOCKS5 (RFC 1928), которые понимает DomainProxy."""

    VERSION = 5
    NO_AUTH = 0
    CONNECT = 1
    DOMAIN = 3


class SocksReply(IntEnum):
    """Коды ответа SOCKS5 на запрос соединения."""

    GRANTED = 0
    NOT_ALLOWED = 2
    REFUSED = 5
    COMMAND_UNSUPPORTED = 7
    ADDRESS_UNSUPPORTED = 8

    def frame(self) -> bytes:
        """Ответ целиком: адрес привязки клиенту не нужен и идёт нулями."""
        return bytes((SocksByte.VERSION, self, 0, 1)) + bytes(6)


class TunnelServer(socketserver.ThreadingTCPServer):
    """TCP-сервер стенда теста: соединение на поток, закрытие ждёт потоки.

    На нём стоят DomainProxy и NegotiateProbe.
    """

    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True


class DomainProxy:
    """SOCKS5-прокси между браузером и стендом: домен стенда ведёт на loopback.

    SPN запроса chromium собирает из имени, в которое разрешился хост адреса,
    а домен стенда на машине теста разрешается не в неё. Через прокси браузер
    имя не разрешает вовсе: соединение с `<домен>:<порт>` прокси отдаёт на
    тот же порт loopback, чужие имена отклоняет. Поднимает его фикстура
    domain_proxy, адрес получает запуск браузера в kerberos_browser.
    """

    CHUNK: ClassVar[int] = 65536
    POLL_SEC: ClassVar[float] = 0.2

    def __init__(self, domain: str) -> None:
        self._domain = domain
        self._port = free_port()
        self._stopped = threading.Event()
        self._server = self._build()
        self._thread = threading.Thread(target=self._server.serve_forever)

    def _build(self) -> TunnelServer:
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy.serve(self.request)

        return TunnelServer((StandUrl.HOST.value, self._port), Handler)

    def __enter__(self) -> DomainProxy:
        self._thread.start()
        return self

    def __exit__(self, *error: object) -> None:
        self._stopped.set()
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()

    def address(self) -> str:
        return f"socks5://{StandUrl.HOST.value}:{self._port}"

    def serve(self, client: socket.socket) -> None:
        """Одно соединение браузера: приветствие, запрос, затем туннель."""
        methods = self._take(client, 2)[1]
        self._take(client, methods)
        client.sendall(bytes((SocksByte.VERSION, SocksByte.NO_AUTH)))

        request = self._take(client, 4)
        if request[1] != SocksByte.CONNECT:
            client.sendall(SocksReply.COMMAND_UNSUPPORTED.frame())
            return

        if request[3] != SocksByte.DOMAIN:
            client.sendall(SocksReply.ADDRESS_UNSUPPORTED.frame())
            return

        size = self._take(client, 1)[0]
        host = self._take(client, size).decode("ascii")
        port = int.from_bytes(self._take(client, 2), "big")
        if host != self._domain:
            client.sendall(SocksReply.NOT_ALLOWED.frame())
            return

        try:
            upstream = socket.create_connection((StandUrl.HOST.value, port))
        except ConnectionRefusedError:
            client.sendall(SocksReply.REFUSED.frame())
            return

        with upstream:
            client.sendall(SocksReply.GRANTED.frame())
            self._pump(client, upstream)

    def _take(self, client: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = client.recv(size - len(data))
            if not chunk:
                msg = (
                    f"socks proxy for {self._domain}: the client closed the "
                    f"connection after {len(data)} of {size} handshake bytes"
                )
                raise ConnectionError(msg)

            data.extend(chunk)

        return bytes(data)

    def _pump(self, client: socket.socket, upstream: socket.socket) -> None:
        """Гонит байты в обе стороны, пока одна не закроется или прокси не снят."""
        with selectors.DefaultSelector() as selector:
            selector.register(client, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, client)

            while not self._stopped.is_set():
                for key, _ in selector.select(timeout=self.POLL_SEC):
                    if not self._forward(key):
                        return

    def _forward(self, key: selectors.SelectorKey) -> bool:
        source = key.fileobj
        target = key.data
        if not isinstance(source, socket.socket):
            raise TypeError(f"socks proxy: expected a socket, got {source!r}")

        if not isinstance(target, socket.socket):
            raise TypeError(f"socks proxy: expected a socket, got {target!r}")

        # браузер рвёт соединение сбросом, когда закрывает вкладку
        try:
            data = source.recv(self.CHUNK)
        except ConnectionResetError:
            return False

        if not data:
            return False

        target.sendall(data)
        return True


class NegotiateProbe:
    """Пробный сервер Negotiate: будит схему в браузере и проверяет её.

    Отвечает 401 Negotiate и запоминает пришедшие токены. Chromium на Linux
    подключает GSSAPI только после первого вызова Negotiate в жизни процесса,
    и на сам этот вызов токена не шлёт: без пробы первый вход теста остался
    бы без билета. Пробу проходит фикстура kerberos_browser.
    """

    SCHEME: ClassVar[str] = "Negotiate "
    VISITS: ClassVar[int] = 3

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.port = free_port()
        self._server = self._build()
        self._thread = threading.Thread(target=self._server.serve_forever)

    def _build(self) -> TunnelServer:
        probe = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                auth = self.headers.get("Authorization", "")
                if auth.startswith(probe.SCHEME):
                    probe.tokens.append(auth)

                body = b"<html><body>probe</body></html>"
                if auth.startswith(probe.SCHEME):
                    self.send_response(200)
                else:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", "Negotiate")

                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        return TunnelServer((StandUrl.HOST.value, self.port), Handler)

    def __enter__(self) -> NegotiateProbe:
        self._thread.start()
        return self

    def __exit__(self, *error: object) -> None:
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()

    def url(self) -> str:
        """Пробный адрес доменным именем: SPN токена совпадает с боевым."""
        return f"http://{STAND.krb_domain}:{self.port}/"

    def awaken(self, browser: Browser) -> None:
        """Ходит на пробу, пока браузер не пришлёт токен; без токена — отказ."""
        context = browser.new_context()
        try:
            page = context.new_page()
            for _ in range(self.VISITS):
                page.goto(self.url(), wait_until="domcontentloaded")
                if self.tokens:
                    return
        finally:
            context.close()

        msg = (
            f"kerberos browser: chromium {browser.version} sent no Negotiate token "
            f"in {self.VISITS} visits of {self.url()}; expected a ticket for "
            f"HTTP/{STAND.krb_domain} from the ccache of {STAND.reader_principal}"
        )
        raise RuntimeError(msg)


@pytest.fixture(scope="module")
def user_ccache(tmp_path_factory: pytest.TempPathFactory) -> str:
    """TGT пользователя стенда: его же берёт браузер, как на рабочей машине."""
    workdir = tmp_path_factory.mktemp("browser-krb")
    ccache = f"FILE:{workdir / 'ccache'}"

    context = krb5.init_context()
    user = krb5.parse_name_flags(context, STAND.reader_principal.encode())
    options = krb5.get_init_creds_opt_alloc(context)
    krb5.get_init_creds_opt_set_forwardable(options, True)
    tgt = krb5.get_init_creds_password(
        context, user, options, STAND.reader_password.get_secret_value().encode()
    )

    cache = krb5.cc_resolve(context, ccache.encode())
    krb5.cc_initialize(context, cache, user)
    krb5.cc_store_cred(context, cache, tgt)

    return ccache


@pytest.fixture(scope="module")
def sso_stand(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    """Стенд только с SSO: локального входа нет, значит вход идёт по тикету."""
    workdir = tmp_path_factory.mktemp("sso-delegation")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-krb",
        auth=StandAuth.SSO,
        sso_roles={STAND.reader_principal: list(StandConfig.STAND_ROLES["admin"])},
    )
    process = StandProcess(config=config, log_path=workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def domain_proxy() -> Iterator[DomainProxy]:
    with DomainProxy(STAND.krb_domain) as proxy:
        yield proxy


@pytest.fixture(scope="module")
def kerberos_browser(
    user_ccache: str,
    domain_proxy: DomainProxy,
    sso_stand: StandProcess,
    playwright: Playwright,
) -> Iterator[Browser]:
    """Chromium, который умеет Negotiate: свой ccache и доверие домену стенда."""
    domain = STAND.krb_domain
    origin = f"http://{domain}:{sso_stand.config.app_port}"
    args = [
        "--no-sandbox",
        f"--auth-server-allowlist=*{domain}",
        f"--auth-negotiate-delegate-allowlist=*{domain}",
        # cookie входа идёт с Secure, а стенд отвечает по http не с loopback
        f"--unsafely-treat-insecure-origin-as-secure={origin}",
    ]
    env: dict[str, str | float | bool] = {
        "KRB5CCNAME": user_ccache,
        "KRB5_CONFIG": STAND.krb_config,
    }

    # channel: headless-shell собран вовсе без сетевой аутентификации
    try:
        instance = playwright.chromium.launch(
            channel="chromium",
            args=args,
            env=env,
            proxy={"server": domain_proxy.address()},
        )
    except PlaywrightError as exc:
        msg = (
            "kerberos browser: launching the full chromium build (channel "
            "'chromium') failed, the headless shell has no Negotiate; install "
            f"it with 'playwright install chromium': {exc}"
        )
        raise RuntimeError(msg) from exc

    try:
        with NegotiateProbe() as probe:
            probe.awaken(instance)

        yield instance
    finally:
        instance.close()


@pytest.fixture
def sso_context(kerberos_browser: Browser) -> Iterator[BrowserContext]:
    context = kerberos_browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        yield context
    finally:
        context.close()


def _domain_url(stand: StandProcess, path: str = "") -> str:
    """Адрес стенда доменным именем: от него браузер собирает SPN."""
    port = stand.config.app_port
    return f"http://{STAND.krb_domain}:{port}{stand.config.url_prefix}{path}"


def _sign_in(page: Page, stand: StandProcess) -> None:
    page.goto(_domain_url(stand, "/login"), wait_until="domcontentloaded")
    expect(page.locator(SSO_BUTTON)).to_be_visible(timeout=30_000)
    page.locator(SSO_BUTTON).click()
    expect(page.locator(CHAT_INPUT)).to_be_visible(timeout=60_000)


def _delegation_lines(stand: StandProcess) -> list[str]:
    """Строки лога про делегирование: по ним виден исход входа."""
    path = stand.log_path
    if not path.is_file():
        return []

    text = path.read_text(encoding="utf-8", errors="replace")

    found: list[str] = []
    for line in text.splitlines():
        for marker in (CAPTURED, NO_DELEGATION, REJECTED):
            if marker in line:
                found.append(line)
                break

    return found


def _visit(connection: HttpConnection, request: HttpRequest) -> None:
    """Ходит по адресу и дочитывает тело; редирект входа — штатный ответ."""

    async def run() -> None:
        async with (
            HttpTransport(connection, HttpTransportConfig()) as transport,
            transport.fetch(request) as got,
        ):
            await got.stream.read()

    try:
        run_blocking(run())
    except HttpStatusError as exc:
        if exc.status != httpx.codes.SEE_OTHER:
            raise


def test_server_accepts_negotiate_and_keeps_the_delegated_ticket(
    sso_stand: StandProcess,
) -> None:
    """Сервер и SPN исправны: вход по билету принят, делегирование сохранено.

    Проверка идёт до браузера: если она зелёная, а браузерная — нет, дело в
    браузере, а не в приложении.
    """
    # адрес — сам стенд, SPN — доменный (service_host): имя домена резолвится
    # не на машину теста
    connection = HttpConnection(
        scheme=UrlScheme.HTTP,
        host=StandUrl.HOST.value,
        port=sso_stand.config.app_port,
        path=sso_stand.config.url_prefix,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=KerberosPasswordAuth(
                method="kerberos_password",
                principal=STAND.reader_principal,
                password=STAND.reader_password,
            ),
            service_host=STAND.krb_domain,
        ),
    )

    _visit(connection, HttpRequest(url="/auth/sso"))

    captured = [line for line in _delegation_lines(sso_stand) if CAPTURED in line]
    if not captured:
        raise AssertionError(
            f"сервер не получил делегированных кредов: {sso_stand.tail()}"
        )
    if STAND.reader_principal not in captured[-1]:
        raise AssertionError(f"тикет достался не тому принципалу: {captured[-1]}")


def test_sso_sign_in_brings_a_delegated_ticket(
    sso_context: BrowserContext, sso_stand: StandProcess
) -> None:
    """Вход браузером: SPNEGO принят и делегированный тикет входа сохранён."""
    page = sso_context.new_page()

    _sign_in(page, sso_stand)

    lines = _delegation_lines(sso_stand)
    if not lines:
        raise AssertionError(f"вход не оставил следа делегирования: {sso_stand.tail()}")

    captured = [line for line in lines if CAPTURED in line]
    if not captured:
        raise AssertionError(f"делегированного тикета вход не принёс: {lines}")

    if STAND.reader_principal not in captured[-1]:
        raise AssertionError(f"тикет достался не тому принципалу: {captured[-1]}")


def test_signed_in_session_carries_the_sealed_ticket(
    sso_context: BrowserContext, sso_stand: StandProcess
) -> None:
    """Запечатанный билет лежит в JWT сессии: из него инструмент получит креды."""
    page = sso_context.new_page()
    _sign_in(page, sso_stand)

    present: dict[str, str] = {}
    for cookie in sso_context.cookies():
        present[str(cookie.get("name", ""))] = str(cookie.get("value", ""))

    # токен с билетом длиннее одной cookie и приходит чанками
    token = CookieJar(SESSION_COOKIE).token_of(present)
    if token is None:
        raise AssertionError(f"после входа нет cookie сессии: {sorted(present)}")

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = SessionClaims.parse(json.loads(base64.urlsafe_b64decode(payload)))
    sign_in = claims.sign_in()

    if sign_in.principal != STAND.reader_principal:
        raise AssertionError(f"в сессии не тот принципал: {sign_in.principal!r}")

    if not sign_in.sealed_ticket:
        raise AssertionError(f"в сессии нет билета входа: {sorted(claims.metadata)}")
