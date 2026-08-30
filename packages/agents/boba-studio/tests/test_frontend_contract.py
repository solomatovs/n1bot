"""Литералы контракта входа во фронте studio совпадают с core: метка своего запроса и
коды исхода SSO. Маршруты входа не попадают в openapi, поэтому типы для них не
генерируются — дрейф ловит этот тест."""

from __future__ import annotations

from pathlib import Path

from boba.identity.sso import OwnRequest, SsoErrorCode

WEB = Path(__file__).resolve().parents[1] / "web" / "workflow" / "src"


def test_own_request_mark_matches_the_client() -> None:
    client = (WEB / "api" / "client.ts").read_text(encoding="utf-8")

    assert f'header: "{OwnRequest.HEADER}"' in client
    assert f'value: "{OwnRequest.VALUE}"' in client


def test_sso_error_codes_are_all_explained_on_the_login_page() -> None:
    page = (WEB / "pages" / "LoginPage.tsx").read_text(encoding="utf-8")

    for code in SsoErrorCode:
        assert f"{code.value}:" in page, f"login page lacks a text for {code.value}"
