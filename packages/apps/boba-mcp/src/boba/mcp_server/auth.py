"""Вход в сервис исполнения: сервер авторизации OAuth 2.1 на fastmcp над ядром
входа boba.

Способы входа, провайдеры и маппинги ролей — те же, что у чата: секции
[auth.*] конфига сервиса и ядро входа SignIns. Слой сервиса — AuthServer,
наследник OAuthProvider fastmcp: local и ldap входят формой на шаге
authorize, kerberos — обменом SPNEGO там же, proxy — обменом утверждения
доверенного клиента на токен (grant jwt-bearer). Итог любого входа — токен
доступа сервиса (ServiceTokens): JWT с логином, ролями и областями —
endpoint'ами, выданными ролям вошедшего. Endpoint проверяет токен своим
EndpointTokens и требует свою область; 401 и 403 отвечает fastmcp. Готовые
токены секции [mcp.tokens] принимаются наравне. Вход человека продлевается
токеном обновления до потолка сессии; все входы разом снимает смена
поколения сессий (рестарт либо generation в конфиге). Cookie и строк
пользователей у сервиса нет; коды авторизации и клиенты динамической
регистрации живут без хранения: начатые входы и коды — в памяти процесса,
запись зарегистрировавшегося клиента — в его же идентификаторе.

Ошибки:
TokenClaimsError — токен принят, но логина и ролей вызывающего в нём нет.
CallScopeError — служебные поля запроса в _meta не проходят модель либо
    область вызова не годится сегментом пути.
AuthorizeError — запрос авторизации не принят (по контракту OAuthProvider).
TokenError — код авторизации или утверждение не приняты (по контракту
    OAuthProvider).
страницы входа отвечают по HTTP: 400 — вход не начат или истёк; 401 — логин
    или пароль неверен либо нужен билет kerberos; 403 — вход запрещён.
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth import (
    AccessToken,
    OAuthProvider,
    RemoteAuthProvider,
    TokenVerifier,
)
from fastmcp.server.auth.auth import TokenHandler
from fastmcp.server.auth.jwt_issuer import JWTIssuer, derive_jwt_key
from fastmcp.server.dependencies import get_access_token, get_context
from joserfc.errors import JoseError
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import (
    JWT_BEARER_GRANT_TYPE,
    OAuthClientInformationFull,
    OAuthToken,
)
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from boba.auth import SignIns
from boba.auth.proxy import AssertionRejectedError, ProxyAssertions
from boba.identity.context import Credential, Scope, Subject
from boba.identity.errors import AuthenticationError, AuthorizationError
from boba.identity.signin import ProxyRequest, SignedIn, SignInMetadata
from boba.identity.sso import SsoChallenge
from boba.runtime.http import SsoRequests, SsoResponses
from boba.toolkit.wire import RequestFields, RequestMeta

__all__ = [
    "AuthServer",
    "CallScopeError",
    "CallScopes",
    "EndpointTokens",
    "LoginPages",
    "McpClient",
    "RegisteredClients",
    "SealedValues",
    "SentMeta",
    "ServiceAuth",
    "ServiceTokens",
    "TokenClaim",
    "TokenClaims",
    "TokenClaimsError",
    "TokenSubjects",
]

logger = logging.getLogger(__name__)


class TokenClaim(StrEnum):
    """Клеймы токена доступа, по которым сервис узнаёт вызывающего."""

    LOGIN = "login"
    ROLES = "roles"
    USER_ID = "user_id"
    METADATA = "metadata"


class TokenClaimsError(Exception):
    """Токен принят, но вызывающего по нему не узнать."""


class TokenClaims(BaseModel):
    """Клеймы вызывающего в токене доступа.

    login и roles обязательны. user_id несёт готовый токен конфига, у
    которого он задан; без него сервис выводит идентификатор из логина.
    metadata — то, что вход знает о себе (SignInMetadata, как в токене
    чата): провайдер, принципал и запечатанный билет kerberos, поколение
    сессий; у готового токена конфига её нет.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    roles: frozenset[str]
    user_id: UUID | None = None
    metadata: Mapping[str, object] = Field(default_factory=dict)


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
        user_id = claims.user_id
        if user_id is None:
            user_id = uuid5(NAMESPACE_URL, f"boba-mcp:{claims.login}")

        return Subject(
            user_id=user_id,
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
    """Клиент прислал служебные поля запроса, с которыми вызов не принять:
    область не годится сегментом пути либо поля не проходят свою модель."""


class SentMeta:
    """Служебные поля текущего запроса tools/call — единственный читатель
    _meta запроса.

    Создаётся теми, кому нужны поля клиента: областью вызова (CallScopes) и
    инструментом реестра (McpTool). Читает _meta запроса из контекста
    fastmcp и разбирает его моделью RequestFields.
    """

    def sent(self) -> RequestFields:
        """Поля запроса; запрос без _meta — пустые поля.

        Ошибки:
        CallScopeError — поля клиента не проходят модель RequestFields.
        """
        request = get_context().request_context
        if request is None:
            return RequestFields()

        meta = request.meta
        if meta is None:
            return RequestFields()

        try:
            return RequestFields.model_validate(meta)
        except ValidationError as exc:
            msg = (
                f"reading _meta of the call: expected "
                f"{RequestMeta.CALL_ID.value!r} and {RequestMeta.SCOPE.value!r} "
                f"as strings, got {dict(meta)!r}: {exc}"
            )
            raise CallScopeError(msg) from exc


class CallScopes:
    """Область вызова по _meta запроса.

    Создаётся сборкой сервера, зовёт её CallContextMiddleware. Область —
    место файлов и описаний вызова в workspace пользователя; клиент с
    собственными разговорами (чат) присылает в ней id разговора, чтобы
    вызовы одного разговора видели файлы друг друга. Клиент без области
    получает область по умолчанию — id самого пользователя, то есть свою
    у каждого пользователя.
    """

    def __init__(self) -> None:
        self._meta = SentMeta()

    def of(self, subject: Subject) -> Scope:
        """Область текущего запроса; негодный id области — CallScopeError."""
        sent = self._meta.sent().scope
        if not sent:
            return Scope.chat(str(subject.user_id))

        try:
            return Scope.chat(sent)
        except ValidationError as exc:
            msg = (
                f"resolving the scope of the call: _meta[{RequestMeta.SCOPE.value!r}] "
                f"expects one path segment, got {sent!r}: {exc}"
            )
            raise CallScopeError(msg) from exc


class McpClient(BaseModel):
    """Клиент OAuth из конфига [mcp.clients.<client_id>]: доверенное
    приложение, которому разрешён обмен утверждения о пользователе на токен
    (вход proxy). Клиенты с входом человека регистрируются сами (DCR)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret: SecretStr

    def registered(self, client_id: str) -> OAuthClientInformationFull:
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=self.secret.get_secret_value(),
            redirect_uris=None,
            grant_types=[JWT_BEARER_GRANT_TYPE],
            token_endpoint_auth_method="client_secret_post",  # noqa: S106 — имя метода
        )


@dataclass(frozen=True)
class TokenHolder:
    """Кому выпускается токен сервиса: логин, то, что вход знает о себе
    (провайдер, роли, выданные endpoint'ы, билет kerberos), и момент
    первого входа, от которого считается потолок сессии."""

    login: str
    sign_in: SignInMetadata
    started: int

    def scopes(self) -> list[str]:
        """Области токена: endpoint'ы, выданные ролям вошедшего."""
        return sorted(self.sign_in.profiles)


class RefreshClaims(BaseModel):
    """Клеймы токена обновления: держатель сессии и metadata его входа."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    metadata: Mapping[str, object]
    started: int


class ServiceTokens:
    """Токены сервиса: выпуск по итогу входа, чтение и продление.

    Создаётся сборкой процесса из публичного адреса сервиса и секции
    [session]; выпускает сервер авторизации (AuthServer), читают проверяющие
    endpoint'ов (EndpointTokens). Токен — JWT fastmcp (JWTIssuer): издатель и
    получатель — сам сервис, области — endpoint'ы, выданные ролям вошедшего,
    логин и роли — клеймами TokenClaim. Какой бы способ входа ни сработал,
    токен один и тот же и несёт metadata входа, как токен чата: провайдер,
    принципал и запечатанный билет kerberos — им инструменты ходят от имени
    пользователя. Проверка локальная, по подписи.

    Токен доступа живёт session_ttl_sec. Вход человека получает ещё и токен
    обновления: по нему клиент сам берёт новую пару без формы входа, пока
    от первого входа не прошло session_max_sec. Хранения нет: оба токена
    несут поколение сессий, и токен другого поколения отвергается — рестарт
    процесса или смена generation в конфиге разом снимает все входы.
    """

    SALT: ClassVar[str] = "boba-mcp-access-token"
    REFRESH: ClassVar[str] = "refresh"

    def __init__(
        self, issuer: str, secret: str, ttl_sec: int, max_sec: int, generation: str
    ) -> None:
        key = derive_jwt_key(high_entropy_material=secret, salt=self.SALT)
        self._issuer = JWTIssuer(issuer, issuer, key)
        self._ttl_sec = ttl_sec
        self._max_sec = max_sec
        self._generation = generation

    def holder(self, signed: SignedIn) -> TokenHolder:
        """Держатель токена по итогу входа: сессия начинается сейчас."""
        return TokenHolder(
            login=signed.identifier, sign_in=signed.sign_in, started=int(time.time())
        )

    def access(self, holder: TokenHolder, client_id: str) -> OAuthToken:
        """Токен доступа без обновления: вход proxy повторяет обмен сам."""
        return OAuthToken(
            access_token=self._access(holder, client_id),
            token_type="Bearer",  # noqa: S106 — вид токена, не секрет
            expires_in=self._ttl_sec,
            scope=" ".join(holder.scopes()),
        )

    def session(self, holder: TokenHolder, client_id: str) -> OAuthToken:
        """Токен доступа и токен обновления до потолка сессии держателя;
        TokenError — потолок сессии пройден."""
        left = holder.started + self._max_sec - int(time.time())
        if left <= 0:
            raise TokenError(
                "invalid_grant",
                f"the session of {holder.login!r} started at {holder.started} is "
                f"over its limit of {self._max_sec}s: sign in again",
            )

        refresh = self._issuer.issue_refresh_token(
            client_id=client_id,
            scopes=holder.scopes(),
            jti=uuid4().hex,
            expires_in=left,
            upstream_claims=RefreshClaims(
                login=holder.login,
                metadata=self._metadata(holder),
                started=holder.started,
            ).model_dump(mode="json"),
        )

        return OAuthToken(
            access_token=self._access(holder, client_id),
            token_type="Bearer",  # noqa: S106 — вид токена, не секрет
            expires_in=min(self._ttl_sec, left),
            scope=" ".join(holder.scopes()),
            refresh_token=refresh,
        )

    def _access(self, holder: TokenHolder, client_id: str) -> str:
        return self._issuer.issue_access_token(
            client_id=client_id,
            scopes=holder.scopes(),
            jti=uuid4().hex,
            expires_in=self._ttl_sec,
            subject=holder.login,
            extra_claims={
                TokenClaim.LOGIN.value: holder.login,
                TokenClaim.ROLES.value: sorted(holder.sign_in.roles),
                TokenClaim.METADATA.value: self._metadata(holder),
            },
        )

    def _metadata(self, holder: TokenHolder) -> dict[str, object]:
        """Metadata входа с текущим поколением сессий."""
        return holder.sign_in.issued_at(self._generation).render()

    def read(self, token: str) -> AccessToken | None:
        """Вызывающий по токену доступа; None — токен не принят."""
        try:
            claims = self._issuer.verify_token(token)
        except JoseError as exc:
            logger.info("access token is rejected: %s", exc)
            return None

        issued = self._sign_in(claims.get(TokenClaim.METADATA.value)).generation
        if issued != self._generation:
            logger.info(
                "access token of %r belongs to session generation %r, this "
                "process accepts %r",
                claims.get(TokenClaim.LOGIN.value),
                issued,
                self._generation,
            )
            return None

        return AccessToken(
            token=token,
            client_id=str(claims["client_id"]),
            scopes=str(claims["scope"]).split(),
            expires_at=int(claims["exp"]),
            claims=dict(claims),
        )

    def refreshed(self, token: str) -> RefreshToken | None:
        """Токен обновления глазами OAuthProvider; None — токен не принят."""
        read = self._refresh(token)
        if read is None:
            return None

        claims, _ = read

        return RefreshToken(
            token=token,
            client_id=str(claims["client_id"]),
            scopes=str(claims["scope"]).split(),
            expires_at=int(claims["exp"]),
        )

    def renewed(self, refresh: RefreshToken) -> OAuthToken:
        """Новая пара токенов тому же держателю: тот же вход, новый срок,
        потолок сессии прежний; TokenError — токен обновления не принят."""
        read = self._refresh(refresh.token)
        if read is None:
            raise TokenError(
                "invalid_grant", "the refresh token is not accepted: sign in again"
            )

        _, held = read
        holder = TokenHolder(
            login=held.login,
            sign_in=SignInMetadata.parse(held.metadata),
            started=held.started,
        )

        return self.session(holder, refresh.client_id)

    def _refresh(self, token: str) -> tuple[dict[str, Any], RefreshClaims] | None:
        try:
            claims = self._issuer.verify_token(token, self.REFRESH)
            held = RefreshClaims.model_validate(claims.get("upstream_claims"))
        except (JoseError, ValidationError) as exc:
            logger.info("refresh token is rejected: %s", exc)
            return None

        issued = SignInMetadata.parse(held.metadata).generation
        if issued != self._generation:
            logger.info(
                "refresh token of %r belongs to session generation %r, this "
                "process accepts %r",
                held.login,
                issued,
                self._generation,
            )
            return None

        return claims, held

    @staticmethod
    def _sign_in(metadata: object) -> SignInMetadata:
        if not isinstance(metadata, Mapping):
            return SignInMetadata()

        return SignInMetadata.parse(metadata)


class EndpointTokens(TokenVerifier):
    """Проверка токена на одном endpoint'е MCP: токен сервиса либо готовый
    токен конфига; область endpoint'а обязательна.

    Создаётся сборкой endpoint'ов на каждый endpoint и отдаётся в
    FastMCP(auth=…) внутри RemoteAuthProvider: без токена fastmcp отвечает
    401 со ссылкой на метаданные ресурса, без области endpoint'а — 403
    insufficient_scope.
    """

    def __init__(
        self, tokens: ServiceTokens, static: Mapping[str, AccessToken], endpoint: str
    ) -> None:
        super().__init__(required_scopes=[endpoint])
        self._tokens = tokens
        self._static = dict(static)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Вызывающий по токену; None — токен не принят (401)."""
        static = self._static.get(token)
        if static is not None:
            return static

        return self._tokens.read(token)


ValueT = TypeVar("ValueT", bound=BaseModel)


class SealedValues:
    """Значения входа без хранения: модель шифруется ключом сервиса и уходит
    наружу строкой, обратно читается тем же ключом.

    Создаётся сборкой процесса из секрета [session]; им пользуются реестр
    клиентов и сервер авторизации. Сервису так нечего помнить между
    запросами: запись клиента едет в его идентификаторе, начатый вход — в
    адресе страницы входа, итог входа — в коде авторизации. Рестарт процесса
    и другой инстанс с тем же секретом читают их одинаково. Срок жизни
    значения проверяется при чтении по времени его выпуска.
    """

    SALT: ClassVar[str] = "boba-mcp-sealed-values"
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, secret: str) -> None:
        self._box = Fernet(derive_jwt_key(high_entropy_material=secret, salt=self.SALT))

    def seal(self, value: BaseModel) -> str:
        record = value.model_dump_json()

        return self._box.encrypt(record.encode(self.ENCODING)).decode(self.ENCODING)

    def opened(
        self, sealed: str, model: type[ValueT], ttl_sec: int | None
    ) -> ValueT | None:
        """Значение модели model; None — выпущено не этим сервисом, испорчено
        либо старше ttl_sec."""
        try:
            record = self._box.decrypt(sealed.encode(self.ENCODING), ttl=ttl_sec)
            return model.model_validate_json(record)
        except (InvalidToken, ValidationError) as exc:
            logger.info(
                "sealed %s %.16s… is not accepted: %s",
                model.__name__,
                sealed,
                type(exc).__name__,
            )
            return None


class RegisteredClients:
    """Клиенты OAuth сервера авторизации: объявленные в конфиге и
    зарегистрировавшиеся сами (DCR).

    Создаётся сборкой процесса для сервера авторизации (AuthServer). Запись
    зарегистрировавшегося клиента отдаётся ему же его идентификатором
    client_id (SealedValues): клиент, запомнивший свой идентификатор,
    остаётся известен после рестарта сервиса.
    """

    SEALED: ClassVar[str] = "sealed"

    def __init__(
        self, sealed: SealedValues, declared: Mapping[str, OAuthClientInformationFull]
    ) -> None:
        self._sealed = sealed
        self._declared = dict(declared)

    def register(self, client: OAuthClientInformationFull) -> None:
        """Выдаёт клиенту идентификатор, несущий его же запись регистрации."""
        # запись несёт заглушку идентификатора: настоящий — сама печать записи
        record = client.model_copy(update={"client_id": self.SEALED})
        client.client_id = self._sealed.seal(record)

    def find(self, client_id: str) -> OAuthClientInformationFull | None:
        """Клиент по идентификатору; None — не объявлен и не выдан этим
        сервисом."""
        declared = self._declared.get(client_id)
        if declared is not None:
            return declared

        client = self._sealed.opened(client_id, OAuthClientInformationFull, None)
        if client is None:
            return None

        client.client_id = client_id

        return client


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
    signed: SignedIn
    expires_at: float


class AuthServer(OAuthProvider):
    """Сервер авторизации сервиса: OAuthProvider fastmcp над ядром входа.

    Создаётся сборкой процесса из способов входа SignIns, выпуска токенов
    ServiceTokens, проверки утверждений proxy и клиентов RegisteredClients; его
    маршруты (метаданные, /authorize, /token, /register) сборка endpoint'ов
    ставит в корень приложения, страницы входа — LoginPages. Вход человека:
    authorize() отдаёт адрес страницы входа с начатым входом, та зовёт
    signed_in() и получает адрес возврата с кодом. Вход proxy:
    exchange_identity_assertion(). Вход человека продлевается токеном
    обновления до потолка [session].session_max_sec; вход proxy токена
    обновления не получает — клиент повторяет обмен утверждения сам.

    Состояния между запросами нет: начатый вход и код авторизации —
    запечатанные значения (SealedValues), рестарт посреди входа его не рвёт.
    Код авторизации поэтому не гасится первым обменом, а живёт свои
    CODE_SEC; увести вход им нельзя — обмен требует секрет PKCE клиента,
    начавшего вход.
    """

    LOGIN_PATH: ClassVar[str] = "/login"
    TXN: ClassVar[str] = "txn"
    PENDING_SEC: ClassVar[int] = 300
    CODE_SEC: ClassVar[int] = 60
    TOKEN_PATH: ClassVar[str] = "/token"  # noqa: S105 — путь маршрута
    METADATA_PATH: ClassVar[str] = "/.well-known/oauth-authorization-server"

    def __init__(  # noqa: PLR0913 — сервер собирается всеми своими входами
        self,
        public_url: str,
        sign_ins: SignIns,
        tokens: ServiceTokens,
        assertions: ProxyAssertions | None,
        clients: RegisteredClients,
        sealed: SealedValues,
        resources: Sequence[str],
    ) -> None:
        super().__init__(
            base_url=public_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=False),
        )
        self._public_url = public_url.rstrip("/")
        self._sign_ins = sign_ins
        self._tokens = tokens
        self._assertions = assertions
        self._resources = frozenset(resources)
        self._clients = clients
        self._sealed = sealed

    def sign_ins(self) -> SignIns:
        return self._sign_ins

    def login_url(self, txn: str) -> str:
        return f"{self._public_url}{self.LOGIN_PATH}?{urlencode({self.TXN: txn})}"

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.find(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients.register(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Адрес страницы входа с начатым входом."""
        if client.client_id is None:
            raise AuthorizeError(
                error="invalid_request",
                error_description="authorize: the client has no client_id",
            )

        self._known_resource(params.resource, AuthorizeError)
        txn = self._sealed.seal(PendingLogin(client_id=client.client_id, params=params))

        return self.login_url(txn)

    def pending(self, txn: str) -> PendingLogin | None:
        """Начатый вход по его идентификатору; None — не начат или истёк."""
        return self._sealed.opened(txn, PendingLogin, self.PENDING_SEC)

    def signed_in(self, held: PendingLogin, signed: SignedIn) -> str:
        """Завершает начатый вход: адрес возврата клиента с кодом авторизации."""
        params = held.params
        code = self._sealed.seal(
            GrantedLogin(
                client_id=held.client_id,
                params=params,
                signed=signed,
                expires_at=time.time() + self.CODE_SEC,
            )
        )
        logger.info(
            "sign-in [user=%s] [provider=%s] [roles=%s]",
            signed.identifier,
            signed.sign_in.provider,
            ",".join(sorted(signed.sign_in.roles)),
        )

        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        granted = self._sealed.opened(authorization_code, GrantedLogin, self.CODE_SEC)
        if granted is None:
            return None

        if granted.client_id != client.client_id:
            return None

        params = granted.params

        return AuthorizationCode(
            code=authorization_code,
            scopes=sorted(granted.signed.sign_in.profiles),
            expires_at=granted.expires_at,
            client_id=granted.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=granted.signed.identifier,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        granted = self._sealed.opened(
            authorization_code.code, GrantedLogin, self.CODE_SEC
        )
        if granted is None:
            raise TokenError(
                "invalid_grant", "the authorization code is unknown or has expired"
            )

        return self._tokens.session(
            self._tokens.holder(granted.signed), granted.client_id
        )

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """Вход proxy: утверждение доверенного клиента о пользователе меняется
        на токен сервиса; роли пользователя решают провайдеры [auth.proxy]."""
        if self._assertions is None:
            raise TokenError(
                "unsupported_grant_type",
                "identity assertion: [auth] has no proxy provider configured",
            )

        client_id = str(client.client_id)
        try:
            claims = self._assertions.read(
                params.assertion, client_id, self._public_url
            )
        except AssertionRejectedError as exc:
            logger.warning("%s", exc)
            raise TokenError("invalid_grant", str(exc)) from exc

        self._known_resource(claims.resource, TokenError)
        request = ProxyRequest(login=claims.sub, roles=claims.roles, client=client_id)
        try:
            signed = await self._sign_ins.admit_proxy(request)
        except (AuthenticationError, AuthorizationError) as exc:
            logger.warning("%s", exc)
            raise TokenError("invalid_grant", str(exc)) from exc

        logger.info(
            "proxy sign-in [user=%s] [client=%s] [roles=%s]",
            signed.identifier,
            client_id,
            ",".join(sorted(signed.sign_in.roles)),
        )

        return self._tokens.access(self._tokens.holder(signed), client_id)

    def _known_resource(
        self, resource: str | None, refusal: type[AuthorizeError | TokenError]
    ) -> None:
        if resource is None:
            return

        if resource.rstrip("/") in self._resources:
            return

        known = ", ".join(sorted(self._resources))
        raise refusal(
            "invalid_target",
            f"resource {resource!r} is not an mcp endpoint of this server; "
            f"known: {known}",
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        held = self._tokens.refreshed(refresh_token)
        if held is None:
            return None

        if held.client_id != client.client_id:
            return None

        return held

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Новая пара токенов по токену обновления, без формы входа."""
        return self._tokens.renewed(refresh_token)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._tokens.read(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Маршруты OAuthProvider с обменом утверждения: базовый класс собирает
        /token и метаданные без него."""
        routes: list[Route] = []
        for route in super().get_routes(mcp_path):
            if route.path == self.TOKEN_PATH:
                routes.append(self._token_route())
                continue

            if route.path == self.METADATA_PATH:
                routes.append(self._metadata_route(route))
                continue

            routes.append(route)

        return routes

    def _token_route(self) -> Route:
        handler = TokenHandler(
            provider=self,
            client_authenticator=ClientAuthenticator(self),
            identity_assertion_enabled=True,
        )

        return Route(
            self.TOKEN_PATH,
            endpoint=cors_middleware(handler.handle, ["POST", "OPTIONS"]),
            methods=["POST", "OPTIONS"],
        )

    def _metadata_route(self, route: Route) -> Route:
        metadata = build_metadata(
            AnyHttpUrl(self._public_url),
            None,
            ClientRegistrationOptions(enabled=True),
            RevocationOptions(enabled=False),
            supports_identity_assertion=True,
        )
        handler = MetadataHandler(metadata)

        return Route(
            route.path,
            endpoint=cors_middleware(handler.handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        )


class LoginField(StrEnum):
    """Поля формы входа и её адреса."""

    TXN = "txn"
    LOGIN = "login"
    PASSWORD = "password"  # noqa: S105 — имя поля формы
    SSO = "/login/sso"


class LoginPages:
    """Страницы входа человека на шаге authorize: форма логина и пароля
    (local, ldap) и обмен SPNEGO (kerberos).

    Создаётся сборкой процесса над сервером авторизации; routes() ставятся
    в корень приложения рядом с его маршрутами. Удачный вход завершает
    начатый вход сервера и ведёт браузер на адрес возврата клиента с кодом.
    """

    PAGE: ClassVar[str] = (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>boba-mcp sign-in</title></head><body>"
        "<h1>Sign in to boba-mcp</h1>{error}{form}{sso}</body></html>"
    )
    FORM: ClassVar[str] = (
        '<form method="post" action="{action}">'
        '<input type="hidden" name="txn" value="{txn}">'
        '<p><label>Login <input name="login" autofocus></label></p>'
        '<p><label>Password <input name="password" type="password"></label></p>'
        '<p><button type="submit">Sign in</button></p></form>'
    )
    SSO: ClassVar[str] = '<p><a href="{url}">Sign in with Kerberos</a></p>'
    ERROR: ClassVar[str] = '<p role="alert">{text}</p>'
    NO_TICKET: ClassVar[str] = (
        "kerberos sign-in needs a ticket of your domain session: the browser sent none"
    )
    NO_PROVIDERS: ClassVar[str] = (
        "no sign-in method for people is configured: [auth] of the service "
        "has no local, ldap or kerberos entry"
    )

    def __init__(self, server: AuthServer, public_url: str) -> None:
        self._server = server
        self._sign_ins = server.sign_ins()
        self._public_url = public_url.rstrip("/")
        self._requests = SsoRequests()
        self._responses = SsoResponses()

    def routes(self) -> list[Route]:
        routes = [
            Route(AuthServer.LOGIN_PATH, self.form, methods=["GET"]),
            Route(AuthServer.LOGIN_PATH, self.submit, methods=["POST"]),
        ]
        if self._sign_ins.has_sso():
            routes.append(Route(LoginField.SSO.value, self.sso, methods=["GET"]))

        return routes

    async def form(self, request: Request) -> Response:
        txn = request.query_params.get(LoginField.TXN.value, "")
        if self._server.pending(txn) is None:
            return self._expired()

        return self._page(txn, "", 200)

    async def submit(self, request: Request) -> Response:
        fields = await request.form()
        txn = str(fields.get(LoginField.TXN.value, ""))
        held = self._server.pending(txn)
        if held is None:
            return self._expired()

        login = str(fields.get(LoginField.LOGIN.value, ""))
        password = str(fields.get(LoginField.PASSWORD.value, ""))
        try:
            signed = await self._sign_ins.by_password(login, password)
        except AuthenticationError as exc:
            return self._page(txn, str(exc), 401)
        except AuthorizationError as exc:
            return self._page(txn, str(exc), 403)

        return RedirectResponse(self._server.signed_in(held, signed), status_code=302)

    async def sso(self, request: Request) -> Response:
        txn = request.query_params.get(LoginField.TXN.value, "")
        held = self._server.pending(txn)
        if held is None:
            return self._expired()

        try:
            outcome = await self._sign_ins.exchange().handshake(
                self._requests.of(request)
            )
        except AuthorizationError as exc:
            return self._page(txn, str(exc), 403)

        if isinstance(outcome, SsoChallenge):
            # браузер домена повторит запрос с билетом сам; остальным страница
            # входа остаётся с причиной и формой
            return self._page(txn, self.NO_TICKET, 401, self._responses.headers())

        return RedirectResponse(
            self._server.signed_in(held, outcome.signed), status_code=302
        )

    def _page(
        self,
        txn: str,
        error: str,
        status: int,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        providers = self._sign_ins.providers()
        if not error and not providers.password and not providers.sso:
            error = self.NO_PROVIDERS

        shown = ""
        if error:
            shown = self.ERROR.format(text=html.escape(error))

        form = ""
        if providers.password:
            form = self.FORM.format(
                action=html.escape(f"{self._public_url}{AuthServer.LOGIN_PATH}"),
                txn=html.escape(txn),
            )

        sso = ""
        if providers.sso:
            query = urlencode({AuthServer.TXN: txn})
            url = f"{self._public_url}{LoginField.SSO.value}?{query}"
            sso = self.SSO.format(url=html.escape(url))

        return HTMLResponse(
            self.PAGE.format(error=shown, form=form, sso=sso),
            status_code=status,
            headers=headers,
        )

    def _expired(self) -> Response:
        text = (
            "the sign-in is not started or has expired: start it again from the client"
        )

        return HTMLResponse(
            self.PAGE.format(error=self.ERROR.format(text=text), form="", sso=""),
            status_code=400,
        )


class ServiceAuth:
    """Вход сервиса целиком: сервер авторизации, страницы входа и проверяющие
    токена endpoint'ов.

    Создаётся сборкой процесса; сборка endpoint'ов (McpEndpoints) берёт у
    него провайдер авторизации на каждый endpoint и маршруты входа для корня
    приложения.
    """

    def __init__(
        self,
        public_url: str,
        server: AuthServer,
        tokens: ServiceTokens,
        static: Mapping[str, AccessToken],
    ) -> None:
        self._public_url = public_url
        self._server = server
        self._pages = LoginPages(server, public_url)
        self._tokens = tokens
        self._static = dict(static)

    def published(self) -> str:
        """Префикс пути, под которым сервис виден клиентам: путь публичного
        адреса; без прокси — пустой."""
        return urlsplit(self._public_url).path.rstrip("/")

    def endpoint(self, name: str) -> RemoteAuthProvider:
        """Провайдер авторизации одного endpoint'а: его отдают в
        FastMCP(auth=…); объявляет сервер авторизации в метаданных ресурса."""
        return RemoteAuthProvider(
            token_verifier=EndpointTokens(self._tokens, self._static, name),
            authorization_servers=[AnyHttpUrl(self._public_url)],
            base_url=self._public_url,
        )

    def routes(self) -> list[Route]:
        """Маршруты входа для корня приложения: сервер авторизации, его
        метаданные по пути издателя и страницы входа."""
        routes = self._server.get_routes()
        taken = {route.path for route in routes}
        for route in self._server.get_well_known_routes():
            if route.path in taken:
                continue

            routes.append(route)

        routes.extend(self._pages.routes())

        return routes
