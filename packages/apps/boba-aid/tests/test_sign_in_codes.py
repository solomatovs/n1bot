"""Отказы входа и сессии говорят с пользователем строками переводов chainlit.

Отказ входа по паролю уходит браузеру кодом (detail ответа /login), и у
каждого кода есть строка auth.login.errors на обоих языках; вход сессии,
который больше не годен, показывается строкой auth.session.errors. Сервис
входа здесь — заглушка с одним отказом: недоступный каталог LDAP и
внутренний сбой стенд вызвать не умеет, неверный пароль и отказ без ролей
снимают тесты вида страницы входа на настоящем стенде.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from aid_stand import AppLanguage
from fastapi import HTTPException

from boba.aid.auth.composite import PasswordCallback, SignInCode
from boba.aid.chat.ui_text import TranslationFiles, UiText
from boba.aid.infra.session import SessionSignIn
from boba.auth import AuthService
from boba.auth.signin import SignedIn
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    BaseError,
    ExternalServiceError,
    InternalServiceError,
)

LANGUAGES = ("ru-RU", "en-US")


class RefusingAuth(AuthService):
    """Сервис входа, который отказывает одной и той же ошибкой."""

    def __init__(self, error: BaseError) -> None:
        # от сервиса входа колбэку нужен только sign_in
        self._error = error

    async def sign_in(self, username: str, password: str) -> SignedIn:
        raise self._error


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Заглушка сессионной фикстуры: колбэк входа зовётся без контекста chainlit."""


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (
            AuthenticationError("Invalid username or password"),
            SignInCode.CREDENTIALS,
            401,
        ),
        (AuthorizationError("no roles are admitted"), SignInCode.DENIED, 403),
        (
            ExternalServiceError("ldap", "LDAP server is down"),
            SignInCode.UNAVAILABLE,
            503,
        ),
        (
            InternalServiceError(
                internal_detail="ldap search failed", user_detail=None
            ),
            SignInCode.INTERNAL,
            500,
        ),
    ],
)
async def test_refusal_goes_out_as_a_code(
    error: BaseError, code: SignInCode, status: int
) -> None:
    callback = PasswordCallback(RefusingAuth(error))

    with pytest.raises(HTTPException) as refused:
        await callback.password_auth("alice", "secret")

    if refused.value.status_code != status:
        raise AssertionError(f"status: {refused.value.status_code}")
    if refused.value.detail != code.value:
        raise AssertionError(f"detail: {refused.value.detail!r}")


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_code_is_translated(language: str) -> None:
    """Строка есть в файле самого языка, а не досталась от языка по умолчанию."""
    files = TranslationFiles(AppLanguage.ASSETS.joinpath(*UiText.DIRECTORY))
    texts = files.read(language)

    keys: list[str] = []
    for code in SignInCode:
        keys.append(f"auth.login.errors.{code.value}")

    for reason in SessionSignIn:
        keys.append(reason.value)

    for key in keys:
        node: object = texts
        for part in key.split(UiText.SEPARATOR):
            if not isinstance(node, Mapping):
                raise AssertionError(f"{language}: {key} stops at {part}")

            node = node.get(part)

        if not isinstance(node, str):
            raise AssertionError(f"{language}: no text for {key}")

        if not node:
            raise AssertionError(f"{language}: empty text for {key}")
