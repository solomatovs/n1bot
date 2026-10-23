"""Вход в сервис исполнения: сервер авторизации OAuth 2.1 (boba.oauth) на
endpoint'ах MCP.

Сервер авторизации, токены, печати и страницы входа — общие для служб boba
(пакет boba-oauth); здесь — то, что знает только сервис исполнения: ресурс
токена — адрес endpoint'а MCP, области — endpoint'ы, выданные ролям
вошедшего, субъект и область вызова по токену. Endpoint проверяет токен
своим ResourceTokens: принимает только токен своего ресурса и требует свою
область; 401 и 403 отвечает fastmcp.

Ошибки:
TokenClaimsError — токен принят, но логина и ролей вызывающего в нём нет.
CallScopeError — область вызова (аргумент scope_id) не строка или не годится
    сегментом пути.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from pydantic import ValidationError

from boba.identity.context import Credential, Scope, Subject
from boba.identity.signin import SignInMetadata
from boba.oauth.server import TokenClaims, TokenClaimsError
from boba.toolrun.call_id import CallFields

__all__ = [
    "CallScopeError",
    "CallScopes",
    "ServiceMark",
    "TokenSubjects",
]


class ServiceMark(StrEnum):
    """Как сервис исполнения называет себя серверу авторизации: имя на
    странице входа и соли ключей токенов и печатей."""

    NAME = "boba-mcp"
    TOKEN_SALT = "boba-mcp-access-token"  # noqa: S105 — соль вывода ключа, не секрет
    SEALED_SALT = "boba-mcp-sealed-values"


class TokenSubjects:
    """Субъект вызова по токену доступа.

    Создаётся сервером endpoint'а с его профилем; им пользуются провайдер
    инструментов (по ролям и профилю — список) и middleware контекста (по
    субъекту — контекст вызова).
    """

    def __init__(self, profile: str) -> None:
        self._profile = profile

    def current(self) -> Subject:
        """Субъект токена текущего запроса; без токена — TokenClaimsError."""
        token = get_access_token()
        if token is None:
            msg = (
                "resolving the caller: the request carries no access token, "
                "expected a bearer token with login and roles"
            )
            raise TokenClaimsError(msg)

        return self.of(token)

    def credential(self) -> Credential:
        """Секреты вызова по токену текущего запроса: делегированный билет
        входа kerberos либо причина его отсутствия."""
        token = get_access_token()
        if token is None:
            msg = (
                "resolving the credential of the caller: the request carries "
                "no access token"
            )
            raise TokenClaimsError(msg)

        claims = self._claims(token)

        return SignInMetadata.parse(claims.metadata).credential()

    def of(self, token: AccessToken) -> Subject:
        claims = self._claims(token)

        return Subject(
            user_id=uuid5(NAMESPACE_URL, f"boba-mcp:{claims.login}"),
            login=claims.login,
            roles=claims.roles,
            profile=self._profile,
        )

    @staticmethod
    def _claims(token: AccessToken) -> TokenClaims:
        try:
            return TokenClaims.model_validate(token.claims)
        except ValidationError as exc:
            msg = (
                f"resolving the caller of client {token.client_id!r}: the token "
                f"claims do not carry login and roles: {exc}"
            )
            raise TokenClaimsError(msg) from exc


class CallScopeError(Exception):
    """Клиент прислал вызов, который не принять: область не годится
    сегментом пути."""


class CallScopes:
    """Область вызова по аргументу области (CallFields.SCOPE).

    Создаётся CallContextMiddleware и FileUploadTool. Область — место файлов
    и описаний вызова в workspace пользователя; её идентификатор для сервиса
    непрозрачен: клиент даёт одну область вызовам, которые должны видеть
    файлы друг друга. Клиент с расширением области (ScopeFeature) ставит
    аргумент сам, остальные показывают его модели. Вызов без области получает
    область по умолчанию — id самого пользователя, то есть свою у каждого
    пользователя.
    """

    def of(self, subject: Subject, arguments: Mapping[str, object]) -> Scope:
        """Область вызова субъекта subject по его аргументам arguments: нет
        области или она пуста — область по умолчанию; область не строка или
        негодный id — CallScopeError."""
        sent = arguments.get(CallFields.SCOPE, "")
        if not isinstance(sent, str):
            msg = (
                f"resolving the scope of the call: argument {CallFields.SCOPE!r} "
                f"expects a string, got {type(sent).__name__}"
            )
            raise CallScopeError(msg)

        if not sent:
            return Scope.caller(str(subject.user_id))

        try:
            return Scope.caller(sent)
        except ValidationError as exc:
            msg = (
                f"resolving the scope of the call: argument {CallFields.SCOPE!r} "
                f"expects one path segment, got {sent!r}: {exc}"
            )
            raise CallScopeError(msg) from exc
