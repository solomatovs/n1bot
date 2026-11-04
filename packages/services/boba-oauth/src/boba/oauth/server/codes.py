"""Начатый вход и код авторизации OAuth."""

from __future__ import annotations

from mcp.server.auth.provider import AuthorizationParams
from pydantic import BaseModel, ConfigDict, Field

from boba.identity.signin import SignInResult


class PendingLogin(BaseModel):
    """Начатый вход человека: кто просит (клиент OAuth) и с какими
    параметрами авторизации; едет в адресе страницы входа и ждёт формы или
    обмена SPNEGO."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(min_length=1)
    params: AuthorizationParams


class GrantedLogin(BaseModel):
    """Итог входа человека: кому выдан код авторизации, на каких условиях и
    кто вошёл; едет самим кодом авторизации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(min_length=1)
    params: AuthorizationParams
    signed: SignInResult
    expires_at: float
