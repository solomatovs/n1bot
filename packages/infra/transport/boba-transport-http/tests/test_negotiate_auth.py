"""NegotiateAuth: kerberos-секция web-профиля и граница дампа."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from boba.connections.http import HttpProfile, NegotiateAuth
from boba.kerberos import DelegatedAuth, KerberosError, TicketAuth
from boba.krb import KerberosCredentials
from boba.transport.http import HttpxAuth
from boba.transport.http.auth import HttpxNegotiateAuth

REVEAL = {TicketAuth.REVEAL_SECRETS: True}


def _delegated(login_path: str | None = None) -> NegotiateAuth:
    """Профиль «идёт сам пользователь»: креды даёт вход в приложение."""
    return NegotiateAuth(
        method="negotiate",
        kerberos=DelegatedAuth(method="kerberos_delegated"),
        login_path=login_path,
    )


def _keytab() -> dict[str, object]:
    return {
        "method": "kerberos_keytab",
        "principal": "svc@EXAMPLE.COM",
        "keytab": "/etc/boba/svc.keytab",
    }


class TestNegotiateProfile:
    def test_service_name_comes_from_base_url(self) -> None:
        auth = _delegated()
        profile = HttpProfile(base_url="https://Wiki.example.com:8443/wiki", auth=auth)
        if profile.service_name() != "HTTP@wiki.example.com":
            raise AssertionError(profile.service_name())

    def test_negotiate_without_base_url_is_rejected(self) -> None:
        auth = _delegated()
        with pytest.raises(ValidationError, match="needs base_url"):
            HttpProfile(auth=auth)

    def test_delegated_row_validates(self) -> None:
        raw = {
            "base_url": "https://wiki.example.com",
            "auth": {
                "method": "negotiate",
                "kerberos": {"method": "kerberos_delegated"},
            },
        }
        profile = HttpProfile.model_validate(raw)
        if not isinstance(profile.auth, NegotiateAuth):
            raise AssertionError("method=negotiate must build NegotiateAuth")
        if not isinstance(profile.auth.kerberos, DelegatedAuth):
            raise AssertionError("kind=delegated must build DelegatedAuth")

    def test_reveal_refuses_a_keytab(self) -> None:
        profile = HttpProfile.model_validate(
            {
                "base_url": "https://wiki.example.com",
                "auth": {"method": "negotiate", "kerberos": _keytab()},
            }
        )
        with pytest.raises(ValueError, match="may not leave the application"):
            profile.model_dump(mode="json", context=REVEAL)

    def test_ticket_travels_and_reads_back(self) -> None:
        ticket = TicketAuth.of_bytes(
            "u@EXAMPLE.COM", "HTTP@wiki.example.com", b"ccache", 60
        )
        profile = HttpProfile(
            base_url="https://wiki.example.com",
            auth=NegotiateAuth(method="negotiate", kerberos=ticket),
        )
        dumped = profile.model_dump(mode="json", context=REVEAL)
        restored = HttpProfile.model_validate(dumped)

        if not isinstance(restored.auth, NegotiateAuth):
            raise AssertionError("auth must survive the roundtrip")
        if not isinstance(restored.auth.kerberos, TicketAuth):
            raise AssertionError("ticket must survive the roundtrip")
        if restored.auth.kerberos.ccache_bytes() != b"ccache":
            raise AssertionError("ticket bytes must survive the roundtrip")
        if not isinstance(HttpxAuth.of(restored), HttpxNegotiateAuth):
            raise AssertionError("negotiate profile must build HttpxNegotiateAuth")

    def test_ticket_is_masked_without_reveal(self) -> None:
        ticket = TicketAuth.of_bytes("u@R", "HTTP@h", b"secret-bytes", 60)
        profile = HttpProfile(
            base_url="https://h",
            auth=NegotiateAuth(method="negotiate", kerberos=ticket),
        )
        dumped = profile.model_dump(mode="json")
        if dumped["auth"]["kerberos"]["ccache"] != "**********":
            raise AssertionError(f"ticket bytes leaked: {dumped['auth']}")

    def test_other_methods_keep_working(self) -> None:
        profile = HttpProfile.model_validate(
            {"base_url": "https://x", "auth": {"method": "bearer", "token": "t"}}
        )
        if HttpxAuth.of(profile) is None:
            raise AssertionError("bearer auth must still be built")


class _StubCredentials(KerberosCredentials):
    """Креды без KDC: окружение пустое, билет всегда «свежий»."""

    @property
    def principal(self) -> str:
        return "u@R"

    @property
    def ccache(self) -> str:
        return "FILE:/dev/null"

    def env(self) -> dict[str, str]:
        return {}

    def ensure(self) -> None:
        return


class _StubNegotiate(HttpxNegotiateAuth):
    def __init__(self, login_url: str | None) -> None:
        super().__init__(_StubCredentials(), "HTTP@confl", login_url)
        self.issued = 0

    def _header(self) -> str:
        self.issued += 1
        return f"Negotiate token-{self.issued}"


class TestLoginServletFlow:
    """Сервис с login-сервлетом: один Negotiate-логин, дальше cookie сессии."""

    @staticmethod
    def _server(seen: list[httpx.Request]) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/login"):
                if "Negotiate" not in request.headers.get("Authorization", ""):
                    challenge = {"WWW-Authenticate": "Negotiate"}
                    return httpx.Response(401, headers=challenge)
                return httpx.Response(
                    302,
                    headers={"Location": "/", "Set-Cookie": "JSESSIONID=abc; Path=/"},
                )
            if request.headers.get("Cookie") == "JSESSIONID=abc":
                return httpx.Response(200, text="known")
            return httpx.Response(200, text="anonymous")

        return httpx.MockTransport(handle)

    def test_login_once_then_cookie_on_every_request(self) -> None:
        seen: list[httpx.Request] = []
        auth = _StubNegotiate("https://confl/plugins/servlet/kerberos/ntlm/login")
        with httpx.Client(transport=self._server(seen), auth=auth) as client:
            first = client.get("https://confl/rest/api/user/current")
            second = client.get("https://confl/rest/api/space")

        if first.text != "known" or second.text != "known":
            raise AssertionError(f"session cookie must reach both requests: {seen}")
        paths = [request.url.path for request in seen]
        if paths.count("/plugins/servlet/kerberos/ntlm/login") != 1:
            raise AssertionError(f"login must happen once: {paths}")
        if auth.issued != 2:
            raise AssertionError("a fresh token per request is still expected")

    def test_login_refusal_is_an_error(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, headers={"WWW-Authenticate": "Negotiate"})

        auth = _StubNegotiate("https://confl/login")
        auth._header = lambda: "Basic nope"  # type: ignore[method-assign]
        transport = httpx.MockTransport(refuse)
        with (
            httpx.Client(transport=transport, auth=auth) as client,
            pytest.raises(KerberosError, match="refused"),
        ):
            client.get("https://confl/rest/api/user/current")

    def test_cookie_survives_a_followed_redirect(self) -> None:
        """Клиент с follow_redirects проходит 302 сам: cookie берётся из истории."""
        seen: list[httpx.Request] = []
        auth = _StubNegotiate("https://confl/plugins/servlet/kerberos/ntlm/login")
        transport = self._server(seen)
        with httpx.Client(transport=transport, auth=auth, follow_redirects=True) as c:
            response = c.get("https://confl/rest/api/user/current")

        if response.text != "known":
            raise AssertionError(f"session cookie must survive redirects: {seen}")

    def test_login_without_a_cookie_is_an_error(self) -> None:
        def silent(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="no cookie here")

        auth = _StubNegotiate("https://confl/login")
        transport = httpx.MockTransport(silent)
        with (
            httpx.Client(transport=transport, auth=auth) as client,
            pytest.raises(KerberosError, match="set no session cookie"),
        ):
            client.get("https://confl/rest/api/user/current")

    def test_without_login_path_negotiate_goes_on_the_request(self) -> None:
        seen: list[httpx.Request] = []
        auth = _StubNegotiate(None)
        with httpx.Client(transport=self._server(seen), auth=auth) as client:
            client.get("https://confl/rest/api/user/current")

        if len(seen) != 1 or "Negotiate" not in seen[0].headers["Authorization"]:
            raise AssertionError(f"negotiate must be on the request itself: {seen}")

    def test_profile_builds_login_url(self) -> None:
        profile = HttpProfile(
            base_url="https://wiki.example.com/",
            auth=_delegated(login_path="/plugins/servlet/kerberos/ntlm/login"),
        )
        expected = "https://wiki.example.com/plugins/servlet/kerberos/ntlm/login"
        if profile.login_url() != expected:
            raise AssertionError(profile.login_url())
