"""SSO-вход studio: обмен Negotiate на её URL и кнопка SSO на странице входа.

Нужен локальный AD стенда (keytab и krb5.conf площадки) и chromium с Negotiate.
"""

from __future__ import annotations

import http.server
import re
import socketserver
import threading
import time
from collections.abc import Iterator
from typing import ClassVar

import httpx
import krb5
import pytest
from playwright.sync_api import Browser, BrowserContext, Playwright, expect
from studio_ui import BOOT_TIMEOUT_SEC, publish_refresh

from boba.kerberos import KerberosPasswordAuth
from boba.runtime.config import StudioRuntimeConfig
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
from boba.transport.http.profile import HttpConnection, NegotiateAuth

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
        app=StandApp.STUDIO,
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


def _visit(profile: HttpConnection, request: HttpRequest) -> None:
    """Ходит по адресу и дочитывает тело; редирект входа — штатный ответ."""

    async def run() -> None:
        async with HttpTransport(profile) as transport, transport.fetch(request) as got:
            await got.stream.read()

    try:
        run_blocking(run())
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 303:
            raise


def test_studio_accepts_negotiate_on_its_own_url(sso_stand: StandProcess) -> None:
    """Тот же обмен на URL studio: вход принят, делегирование сохранено."""
    profile = HttpConnection(
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
    before = len(_delegation_lines(sso_stand))

    _visit(profile, HttpRequest(url="/api/v1/auth/sso"))

    lines = _delegation_lines(sso_stand)[before:]
    captured = [line for line in lines if CAPTURED in line]
    if not captured:
        raise AssertionError(
            f"studio did not capture the delegation: {sso_stand.tail()}"
        )


def test_studio_login_page_signs_in_with_sso_and_returns(
    sso_context: BrowserContext, sso_stand: StandProcess
) -> None:
    """Кнопка SSO на странице studio: обмен на её URL и возврат туда, откуда ушли."""
    page = sso_context.new_page()
    page.goto(
        _domain_url(sso_stand, "/workflow/account"), wait_until="domcontentloaded"
    )
    expect(page).to_have_url(re.compile(r"/workflow/login$"), timeout=30_000)

    page.get_by_role("link", name="Sign in with SSO").click()

    expect(page).to_have_url(re.compile(r"/workflow/account$"), timeout=60_000)
    expect(page.locator(".account__login")).to_be_visible(timeout=30_000)


REFRESHED = "kerberos: refreshed sign-in ticket"
"""Строка лога повторного обмена: страница молча обновила билет по сигналу."""


def _wait_for_log(stand: StandProcess, marker: str, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if stand.log_path.is_file() and marker in stand.log_path.read_text(
            encoding="utf-8", errors="replace"
        ):
            return True

        time.sleep(0.5)

    return False


def _session_cookie(context: BrowserContext, name: str) -> str:
    for cookie in context.cookies():
        if cookie.get("name") == name:
            return str(cookie.get("value"))

    return ""


def test_refresh_signal_makes_the_page_refresh_its_ticket(
    sso_context: BrowserContext,
    sso_stand: StandProcess,
    studio_config: StudioRuntimeConfig,
    stand_database: str,
) -> None:
    """Сигнал в область пользователя: страница шлёт POST refresh со своей меткой,
    браузер отвечает Negotiate, сессия получает новую cookie с новым билетом.
    """
    page = sso_context.new_page()
    page.goto(_domain_url(sso_stand, "/workflow/login"), wait_until="domcontentloaded")
    page.get_by_role("link", name="Sign in with SSO").click()
    expect(page).to_have_url(re.compile(r"/workflow/workflow$"), timeout=60_000)
    expect(page.locator(".lamp").first).to_be_visible(timeout=30_000)

    me = page.request.get(_domain_url(sso_stand, "/api/v1/me")).json()
    cookie_name = studio_config.session.cookie
    before = _session_cookie(sso_context, cookie_name)
    assert before, "sign-in must set the session cookie"

    publish_refresh(
        studio_config, stand_database, str(me["id"]), STAND.reader_principal
    )

    assert _wait_for_log(sso_stand, REFRESHED, 30.0), sso_stand.tail()
    page.wait_for_timeout(500)
    after = _session_cookie(sso_context, cookie_name)
    assert after, "refresh must keep the session cookie"
    assert after != before, "refresh must issue a new session cookie"
