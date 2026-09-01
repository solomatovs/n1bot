"""Вход через SPNEGO: доходит ли до приложения делегированный тикет пользователя.

Сначала проверяется сервер клиентом, который Negotiate умеет заведомо, затем —
браузером. Браузер здесь настоящий: chromium берёт TGT из ccache и проходит
обмен сам, а домен стенда подменяется его резолвером, иначе SPN запроса не
совпал бы с тем, на который выдан keytab приложения.

Сборка chromium у playwright собрана без внешней аутентификации и на вызов
`Negotiate` отвечает ERR_UNSUPPORTED_AUTH_SCHEME, поэтому браузерная часть
пропускается, когда браузер этой схемы не знает: проверка остаётся для машин
с обычным chrome.
"""

from __future__ import annotations

import base64
import http.server
import json
import socketserver
import threading
from collections.abc import Iterator
from typing import ClassVar

import httpx
import krb5
import pytest
from chat_ui import BOOT_TIMEOUT_SEC
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect

from boba.kerberos import KerberosPasswordAuth
from boba.stand.site import Stand
from boba.stand.ui.database import run_blocking
from boba.stand.ui.stand import (
    StandApp,
    StandAuth,
    StandConfig,
    StandProcess,
    free_port,
)
from boba.transport.http import HttpRequest, HttpTransport
from boba.transport.http.profile import HttpProfile, NegotiateAuth

pytestmark = pytest.mark.ui

STAND = Stand.required()

SSO_BUTTON = "#sso-login-btn"
CHAT_INPUT = "#chat-input"

CAPTURED = "captured delegated credentials"
"""Строка лога успешного захвата: по ней видно, что делегирование доехало."""

NO_DELEGATION = "no delegated_credentials"
"""Строка лога, когда accept прошёл, а evidence-кредов KDC не дал."""

REJECTED = "delegated credentials of"
"""Строка лога, когда креды пришли, но не подошли режиму делегирования."""


class NegotiateProbe:
    """Проба браузера: отвечает 401 Negotiate и смотрит, вернётся ли токен.

    Без неё браузерный тест падал бы там, где браузер просто не знает схемы.
    """

    SCHEME: ClassVar[str] = "Negotiate "

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.port = free_port()
        self._server = self._build()

    def _build(self) -> socketserver.TCPServer:
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
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        return socketserver.TCPServer(("127.0.0.1", self.port), Handler)

    def __enter__(self) -> NegotiateProbe:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *error: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def url(self) -> str:
        """Пробный адрес доменным именем: браузер резолвит его на себя."""
        return f"http://{STAND.krb_domain}:{self.port}/"


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
def kerberos_browser(user_ccache: str, playwright: Playwright) -> Iterator[Browser]:
    """Chromium, который умеет Negotiate: свой ccache и доверие домену стенда."""
    domain = STAND.krb_domain
    args = [
        "--no-sandbox",
        f"--host-resolver-rules=MAP {domain} 127.0.0.1",
        f"--auth-server-allowlist=*{domain}",
        f"--auth-negotiate-delegate-allowlist=*{domain}",
    ]
    env: dict[str, str | float | bool] = {
        "KRB5CCNAME": user_ccache,
        "KRB5_CONFIG": STAND.krb_config,
    }

    # channel: headless-shell собран вовсе без сетевой аутентификации
    instance = playwright.chromium.launch(channel="chromium", args=args, env=env)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def browser_speaks_negotiate(kerberos_browser: Browser) -> bool:
    """Умеет ли эта сборка браузера схему Negotiate вообще."""
    context = kerberos_browser.new_context()
    try:
        with NegotiateProbe() as probe:
            page = context.new_page()
            page.goto(probe.url(), wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            return bool(probe.tokens)
    finally:
        context.close()


@pytest.fixture
def sso_context(
    kerberos_browser: Browser, browser_speaks_negotiate: bool
) -> Iterator[BrowserContext]:
    if not browser_speaks_negotiate:
        pytest.skip("сборка браузера не поддерживает Negotiate (нужен обычный chrome)")

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


def _visit(profile: HttpProfile, request: HttpRequest) -> None:
    """Ходит по адресу и дочитывает тело; редирект входа — штатный ответ."""

    async def run() -> None:
        async with HttpTransport(profile) as transport, transport.fetch(request) as got:
            await got.stream.read()

    try:
        run_blocking(run())
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 303:
            raise


def test_server_accepts_negotiate_and_keeps_the_delegated_ticket(
    sso_stand: StandProcess,
) -> None:
    """Сервер и SPN исправны: вход по билету принят, делегирование сохранено.

    Проверка идёт до браузера: если она зелёная, а браузерная — нет, дело в
    браузере, а не в приложении.
    """
    profile = HttpProfile(
        base_url=_domain_url(sso_stand),
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

    _visit(profile, HttpRequest(url="/auth/sso"))

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

    token = ""
    for cookie in sso_context.cookies():
        name = str(cookie.get("name", ""))
        if name.startswith("access_token"):
            token = str(cookie.get("value", ""))

    if not token:
        raise AssertionError("после входа нет cookie сессии")

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    metadata = claims.get("user", {}).get("metadata", {})

    if metadata.get("principal") != STAND.reader_principal:
        raise AssertionError(f"в сессии не тот принципал: {metadata}")
    if not metadata.get("sso_ticket"):
        raise AssertionError(f"в сессии нет билета входа: {metadata}")
