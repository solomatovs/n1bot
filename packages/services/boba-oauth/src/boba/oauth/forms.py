"""Поля HTTP-формы входа OAuth: общий контракт сервера и клиента."""

from __future__ import annotations

from enum import StrEnum


class LoginField(StrEnum):
    """Поля формы входа по паролю, её адреса и cookie страницы входа: общий
    контракт сервера со страницей входа (boba-mcp) и клиента, который
    проходит её без человека (boba-mcp-client)."""

    TXN = "txn"
    LOGIN = "login"
    PASSWORD = "password"  # noqa: S105 — имя поля формы
    PROOF = "proof"
    REALM = "realm"
    COOKIE = "boba_mcp_login"
    SSO = "/login/sso"
