"""Одна cookie входа: cookie, поставленная chainlit, читается studio и наоборот."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import ClassVar

import pytest
from starlette.responses import Response

from boba.chainlit.infra.entry import AppEntry
from boba.identity.token import CookieSpec
from boba.runtime.config import ConfigLocator, RuntimeConfig
from boba.runtime.http import SessionCookie

CHAINLIT_SIDE = """
import json, sys
from starlette.requests import Request
from starlette.responses import Response
from chainlit.auth.cookie import get_token_from_cookies, set_auth_cookie
from boba.chainlit.auth.installer import ChainlitSessionTtl

job = json.loads(sys.stdin.read())
ChainlitSessionTtl.apply(job["ttl"])
if job["op"] == "get":
    print(json.dumps(get_token_from_cookies(job["cookies"])))
else:
    header = "; ".join(f"{k}={v}" for k, v in job["cookies"].items()).encode()
    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": [(b"cookie", header)]}
    response = Response()
    set_auth_cookie(Request(scope), response, job["token"])
    print(json.dumps(response.headers.getlist("set-cookie")))
"""


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Тест сравнивает cookie двух приложений: сессия чата ему не нужна."""


class CookieHeaders:
    """Разбор заголовков Set-Cookie в пары имя -> значение и атрибуты."""

    @staticmethod
    def values(headers: list[str]) -> dict[str, str]:
        present: dict[str, str] = {}
        for header in headers:
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            for key, morsel in jar.items():
                if morsel["max-age"] == "0":
                    continue
                present[key] = morsel.value

        return present

    @staticmethod
    def attributes(headers: list[str]) -> list[Mapping[str, str]]:
        found: list[Mapping[str, str]] = []
        for header in headers:
            jar: SimpleCookie = SimpleCookie()
            jar.load(header)
            for morsel in jar.values():
                found.append(
                    {
                        "samesite": morsel["samesite"].lower(),
                        "path": morsel["path"],
                        "max-age": morsel["max-age"],
                    }
                )

        return found


@pytest.mark.integration
class TestSharedSessionCookie:
    LONG_TOKEN: ClassVar[str] = "t" * 7000
    SHORT_TOKEN: ClassVar[str] = "short-token"

    @staticmethod
    def chainlit_side(job: Mapping[str, object]) -> object:
        AppEntry.export_env(ConfigLocator.path())
        proc = subprocess.run(
            [sys.executable, "-c", CHAINLIT_SIDE],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            env=dict(os.environ),
            check=False,
        )
        assert proc.returncode == 0, proc.stderr

        return json.loads(proc.stdout)

    @staticmethod
    def studio_cookie(config: RuntimeConfig) -> SessionCookie:
        session = config.session
        spec = CookieSpec(
            name=session.cookie,
            samesite=session.cookie_samesite,
            ttl_sec=session.session_ttl_sec,
        )

        return SessionCookie(spec)

    @pytest.mark.parametrize("token", [SHORT_TOKEN, LONG_TOKEN], ids=["short", "long"])
    def test_chainlit_cookie_is_read_by_studio(
        self, runtime_config: RuntimeConfig, token: str
    ) -> None:
        ttl = runtime_config.session.session_ttl_sec
        job = {"op": "set", "token": token, "cookies": {}, "ttl": ttl}
        headers = self.chainlit_side(job)
        assert isinstance(headers, list)

        present = CookieHeaders.values(headers)

        assert self.studio_cookie(runtime_config).token_of(present) == token

        for attributes in CookieHeaders.attributes(headers):
            assert attributes["samesite"] == runtime_config.session.cookie_samesite
            assert attributes["path"] == CookieSpec.PATH
            assert attributes["max-age"] == str(ttl)

    @pytest.mark.parametrize("token", [SHORT_TOKEN, LONG_TOKEN], ids=["short", "long"])
    def test_studio_cookie_is_read_by_chainlit(
        self, runtime_config: RuntimeConfig, token: str
    ) -> None:
        response = Response()
        self.studio_cookie(runtime_config).put(response, {}, token)

        present = CookieHeaders.values(response.headers.getlist("set-cookie"))
        ttl = runtime_config.session.session_ttl_sec

        job = {"op": "get", "cookies": present, "ttl": ttl}

        assert self.chainlit_side(job) == token
