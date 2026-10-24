"""Сервер авторизации OAuth 2.1 служб boba на fastmcp над ядром входа.

Способы входа, провайдеры и маппинги ролей — те же, что у чата: секции
[auth.*] конфига службы и ядро входа SignIns. Сервер — AuthServer, наследник
OAuthProvider fastmcp: local и ldap входят формой на шаге authorize, kerberos
— обменом SPNEGO там же, proxy — обменом утверждения доверенного клиента на
токен (grant jwt-bearer). Итог любого входа — токен доступа службы
(ServiceTokens): JWT с логином, ролями, областями — профилями, выданными
ролям вошедшего, — и ресурсом: адресом, для которого токен выпущен. Ресурс
клиент называет при входе (RFC 8707), без него вход не начинается; адрес
принимает только токен своего ресурса (ResourceTokens). Вход человека
продлевается токеном обновления до потолка сессии; все входы разом снимает
смена поколения сессий (рестарт либо generation в конфиге). Cookie сессии и
строк пользователей нет; начатые входы, коды авторизации и клиенты
динамической регистрации живут без хранения, запечатанными значениями:
начатый вход — в адресе страницы входа, итог входа — в коде авторизации,
запись зарегистрировавшегося клиента — в его же идентификаторе. Шаг входа
принимается только со страницы входа, открытой в этом же браузере: её
подтверждение лежит в cookie страницы и в самой странице.

Сервер собирают службы boba-mcp и агент `boba serve`; каждая называет себя
(имя на странице входа, соли ключей токенов и печатей) и свои ресурсы.

Ошибки:
TokenClaimsError — токен принят, но логина и ролей вызывающего в нём нет.
AuthorizeError — запрос авторизации не принят (по контракту OAuthProvider).
TokenError — код авторизации или утверждение не приняты (по контракту
    OAuthProvider).
страницы входа отвечают по HTTP: 400 — вход не начат или истёк; 401 — логин
    или пароль неверен либо нужен билет kerberos; 403 — вход запрещён либо
    шаг входа пришёл не со страницы входа этого браузера.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Self, TypeVar
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from fastmcp.server.auth import (
    AccessToken,
    OAuthProvider,
    RemoteAuthProvider,
    TokenVerifier,
)
from fastmcp.server.auth.auth import TokenHandler
from fastmcp.server.auth.jwt_issuer import JWTIssuer, derive_jwt_key
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
    model_validator,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from boba.auth import SignIns
from boba.auth.proxy import AssertionRejectedError, ProxyAssertions
from boba.auth.signin import LoginField
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
)
from boba.identity.signin import ProxyRequest, SignedIn, SignInMetadata
from boba.identity.sso import SsoChallenge
from boba.runtime.http import SsoRequests, SsoResponses

__all__ = [
    "AuthServer",
    "GrantedLogin",
    "LoginPages",
    "PendingLogin",
    "RegisteredClients",
    "ResourceTokens",
    "SealedValues",
    "ServiceAuth",
    "ServiceTokens",
    "TokenClaim",
    "TokenClaims",
    "TokenClaimsError",
    "TokenHolder",
    "TrustedClient",
]

logger = logging.getLogger(__name__)


class TokenClaim(StrEnum):
    """Клеймы токена доступа, по которым сервис узнаёт вызывающего."""

    LOGIN = "login"
    ROLES = "roles"
    RESOURCE = "resource"
    METADATA = "metadata"


class TokenClaimsError(Exception):
    """Токен принят, но вызывающего по нему не узнать."""


class TokenClaims(BaseModel):
    """Клеймы вызывающего в токене доступа.

    metadata — то, что вход знает о себе (SignInMetadata, как в токене
    чата): провайдер, принципал и запечатанный билет kerberos, поколение
    сессий.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    roles: frozenset[str]
    metadata: Mapping[str, object]


class ServiceAddress(BaseModel):
    """Адрес службы для клиентов: публичный адрес и префикс пути.

    База секции службы ([mcp] у boba-mcp, [agent] у службы агента). Служба
    сама отвечает под url_prefix, прокси передаёт путь как есть; от
    public_url строятся метаданные OAuth, адреса входа и издатель токена,
    поэтому его путь обязан совпадать с префиксом.
    """

    public_url: str = Field(min_length=1)
    """Адрес службы, каким его видят клиенты, с префиксом. Вне localhost —
    только https."""
    url_prefix: str = Field(pattern=r"^(/[^/]+)*$")
    """Префикс путей службы: пусто либо `/имя`, без `/` в конце."""

    @model_validator(mode="after")
    def _prefix_of_public_url(self) -> Self:
        path = urlsplit(self.public_url).path.rstrip("/")
        if path != self.url_prefix:
            msg = (
                f"public_url {self.public_url!r} has the path {path!r}, expected "
                f"the url_prefix {self.url_prefix!r}: the service answers under "
                "url_prefix and builds its OAuth addresses from public_url"
            )
            raise ValueError(msg)

        return self


class TrustedClient(BaseModel):
    """Клиент OAuth из конфига службы ([mcp.clients.<client_id>] у boba-mcp,
    [agent.clients.<client_id>] у службы агента): доверенное приложение,
    которому разрешён обмен утверждения о пользователе на токен (вход proxy).
    Клиенты с входом человека регистрируются сами (DCR)."""

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
    """Кому и для чего выпускается токен службы: логин, то, что вход знает
    о себе (провайдер, роли, выданные профили, билет kerberos), момент
    первого входа, от которого считается потолок сессии, и ресурс — адрес,
    на котором токен будет принят."""

    login: str
    sign_in: SignInMetadata
    started: int
    resource: str

    def scopes(self) -> list[str]:
        """Области токена: профили службы, выданные ролям вошедшего
        (endpoint'ы у boba-mcp, профили агента у службы агента)."""
        return sorted(self.sign_in.profiles)


class RefreshClaims(BaseModel):
    """Клеймы токена обновления: держатель сессии, metadata его входа и
    ресурс, для которого сессия открыта."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(min_length=1)
    metadata: Mapping[str, object]
    started: int
    resource: str = Field(min_length=1)


class ServiceTokens:
    """Токены службы: выпуск по итогу входа, чтение и продление.

    Создаётся сборкой процесса из публичного адреса службы, секции
    [session] и соли ключа своей службы; выпускает сервер авторизации
    (AuthServer), читают проверяющие ресурсов (ResourceTokens). Токен — JWT
    fastmcp (JWTIssuer): издатель и получатель — сама служба, области —
    профили, выданные ролям вошедшего, логин, роли и ресурс — клеймами
    TokenClaim. Ресурс — адрес, названный клиентом при входе: токен годен
    только на нём, на каждый ресурс клиент входит отдельно. Какой бы способ
    входа ни сработал,
    токен один и тот же и несёт metadata входа, как токен чата: провайдер,
    принципал и запечатанный билет kerberos — им инструменты ходят от имени
    пользователя. Проверка локальная, по подписи.

    Токен доступа живёт session_ttl_sec. Вход человека получает ещё и токен
    обновления: по нему клиент сам берёт новую пару без формы входа, пока
    от первого входа не прошло session_max_sec. Хранения нет: оба токена
    несут поколение сессий, и токен другого поколения отвергается — рестарт
    процесса или смена generation в конфиге разом снимает все входы.
    """

    REFRESH: ClassVar[str] = "refresh"

    def __init__(  # noqa: PLR0913 — токены собираются всеми параметрами [session]
        self,
        issuer: str,
        secret: str,
        ttl_sec: int,
        max_sec: int,
        generation: str,
        salt: str,
    ) -> None:
        key = derive_jwt_key(high_entropy_material=secret, salt=salt)
        self._issuer = JWTIssuer(issuer, issuer, key)
        self._ttl_sec = ttl_sec
        self._max_sec = max_sec
        self._generation = generation

    def holder(self, signed: SignedIn, resource: str) -> TokenHolder:
        """Держатель токена по итогу входа: сессия начинается сейчас."""
        return TokenHolder(
            login=signed.identifier,
            sign_in=signed.sign_in,
            started=int(time.time()),
            resource=resource,
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
                resource=holder.resource,
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
                TokenClaim.RESOURCE.value: holder.resource,
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

        resource = claims.get(TokenClaim.RESOURCE.value)
        if not isinstance(resource, str):
            logger.info(
                "access token of %r names no resource, expected the address of "
                "a resource of this service in claim %r: sign in again",
                claims.get(TokenClaim.LOGIN.value),
                TokenClaim.RESOURCE.value,
            )
            return None

        return AccessToken(
            token=token,
            client_id=str(claims["client_id"]),
            scopes=str(claims["scope"]).split(),
            expires_at=int(claims["exp"]),
            resource=resource,
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
            resource=held.resource,
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


class ResourceTokens(TokenVerifier):
    """Проверка токена на одном ресурсе службы: токен службы, выпущенный для
    этого адреса; области scopes обязательны.

    Создаётся входом службы на каждый ресурс (endpoint MCP у boba-mcp, адрес
    ACP у службы агента) и отдаётся в RemoteAuthProvider: без токена и с
    токеном другого ресурса ответ — 401 со ссылкой на метаданные ресурса, без
    обязательной области — 403 insufficient_scope.
    """

    def __init__(
        self, tokens: ServiceTokens, resource: str, scopes: Sequence[str]
    ) -> None:
        super().__init__(required_scopes=list(scopes))
        self._tokens = tokens
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        """Вызывающий по токену; None — токен не принят (401)."""
        read = self._tokens.read(token)
        if read is None:
            return None

        if read.resource != self._resource:
            logger.info(
                "access token of client %r is issued for %r, this resource "
                "accepts tokens for %r",
                read.client_id,
                read.resource,
                self._resource,
            )
            return None

        return read


ValueT = TypeVar("ValueT", bound=BaseModel)


class SealedValues:
    """Значения входа без хранения: модель шифруется ключом сервиса и уходит
    наружу строкой, обратно читается тем же ключом.

    Создаётся сборкой процесса из секрета [session] и соли печатей своей
    службы; им пользуются реестр
    клиентов и сервер авторизации. Сервису так нечего помнить между
    запросами: запись клиента едет в его идентификаторе, начатый вход — в
    адресе страницы входа, итог входа — в коде авторизации. Рестарт процесса
    и другой инстанс с тем же секретом читают их одинаково. Срок жизни
    значения проверяется при чтении по времени его выпуска.
    """

    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self, secret: str, salt: str) -> None:
        self._box = Fernet(derive_jwt_key(high_entropy_material=secret, salt=salt))

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
    """Сервер авторизации службы: OAuthProvider fastmcp над ядром входа.

    Создаётся сборкой процесса службы из способов входа SignIns, выпуска
    токенов ServiceTokens, проверки утверждений proxy, клиентов
    RegisteredClients и адресов её ресурсов; его маршруты (/authorize,
    /token, /register) служба ставит под своим префиксом, метаданные — в
    корень хоста (ServiceAuth.app_routes), страницы входа — LoginPages. Вход человека:
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

        self._resource(params.resource, AuthorizeError)
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

        resource = self._resource(granted.params.resource, TokenError)

        return self._tokens.session(
            self._tokens.holder(granted.signed, resource), granted.client_id
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

        resource = self._resource(claims.resource, TokenError)
        request = ProxyRequest(login=claims.sub, roles=claims.roles, client=client_id)
        try:
            signed = await self._sign_ins.admit_proxy(request)
        except (AuthenticationError, AuthorizationError) as exc:
            logger.warning("%s", exc)
            raise TokenError("invalid_grant", str(exc)) from exc
        except ExternalServiceError as exc:
            logger.error("%s", exc)
            message = (
                f"identity assertion of {claims.sub!r} from {client_id}: the "
                f"service could not decide the roles of the user: {exc}"
            )
            raise TokenError("invalid_grant", message) from exc

        logger.info(
            "proxy sign-in [user=%s] [client=%s] [roles=%s]",
            signed.identifier,
            client_id,
            ",".join(sorted(signed.sign_in.roles)),
        )

        return self._tokens.access(self._tokens.holder(signed, resource), client_id)

    def _resource(
        self, resource: str | None, refusal: type[AuthorizeError | TokenError]
    ) -> str:
        """Адрес ресурса, для которого просят токен, каким его знает
        служба; без ресурса и с чужим ресурсом — отказ refusal."""
        known = ", ".join(sorted(self._resources))
        if resource is None:
            raise refusal(
                "invalid_target",
                "the request names no resource: parameter resource expects the "
                f"address of a resource of this service; known: {known}",
            )

        named = resource.rstrip("/")
        if named in self._resources:
            return named

        raise refusal(
            "invalid_target",
            f"resource {resource!r} is not a resource of this service; known: {known}",
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


@dataclass(frozen=True)
class LoginVisit:
    """Заход браузера на страницу начатого входа: какой вход открыт и какое
    подтверждение страницы браузер прислал в cookie (пусто — не прислал).

    Создаётся страницами входа (LoginPages) на каждый запрос. Подтверждение
    выдаётся странице входа и лежит в двух местах: в cookie браузера и в
    самой странице (поле формы, ссылка kerberos). Шаг входа принимается,
    только если оба совпали, — так вход нельзя завершить ссылкой или формой
    с чужого сайта, минуя страницу с названием приложения и адресом возврата.
    """

    txn: str
    held: PendingLogin
    cookie: str

    def confirmed(self, proof: str) -> bool:
        if not self.cookie:
            return False

        if not proof:
            return False

        return secrets.compare_digest(
            self.cookie.encode(SealedValues.ENCODING),
            proof.encode(SealedValues.ENCODING),
        )


class LoginPages:
    """Страницы входа человека на шаге authorize: форма логина и пароля
    (local, ldap) и обмен SPNEGO (kerberos).

    Создаётся сборкой процесса над сервером авторизации; routes() ставятся
    в корень приложения рядом с его маршрутами. Удачный вход завершает
    начатый вход сервера и ведёт браузер на адрес возврата клиента с кодом.
    Страница называет службу (service), приложение, начавшее вход, и адрес
    возврата; шаг входа без подтверждения этой страницы (LoginVisit) не
    принимается.
    """

    PAGE: ClassVar[str] = (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>{service} sign-in</title></head><body>"
        "<h1>Sign in to {service}</h1>{error}{client}{form}{sso}</body></html>"
    )
    CLIENT: ClassVar[str] = (
        "<p>Application <b>{name}</b> asks to sign in to {service} as you. "
        "After the sign-in this browser is sent to <b>{redirect}</b>. "
        "Continue only if you started this sign-in yourself.</p>"
    )
    UNNAMED: ClassVar[str] = "without a name"
    FORM: ClassVar[str] = (
        '<form method="post" action="{action}">'
        '<input type="hidden" name="txn" value="{txn}">'
        '<input type="hidden" name="proof" value="{proof}">'
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
    UNCONFIRMED: ClassVar[str] = (
        "this sign-in step did not come from the sign-in page of this browser: "
        "check the application below and sign in on this page"
    )
    HEADERS: ClassVar[Mapping[str, str]] = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
    }

    def __init__(self, server: AuthServer, public_url: str, service: str) -> None:
        self._server = server
        self._service = html.escape(service)
        self._sign_ins = server.sign_ins()
        self._public_url = public_url.rstrip("/")
        public = urlsplit(self._public_url)
        self._cookie_path = f"{public.path}{AuthServer.LOGIN_PATH}"
        self._cookie_secure = public.scheme == "https"
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
        visit = self._visit(request, request.query_params.get(LoginField.TXN.value, ""))
        if visit is None:
            return self._expired()

        return await self._page(visit, "", 200)

    async def submit(self, request: Request) -> Response:
        fields = await request.form()
        visit = self._visit(request, str(fields.get(LoginField.TXN.value, "")))
        if visit is None:
            return self._expired()

        if not visit.confirmed(str(fields.get(LoginField.PROOF.value, ""))):
            return await self._page(visit, self.UNCONFIRMED, 403)

        login = str(fields.get(LoginField.LOGIN.value, ""))
        password = str(fields.get(LoginField.PASSWORD.value, ""))
        try:
            signed = await self._sign_ins.by_password(login, password)
        except AuthenticationError as exc:
            return await self._page(visit, str(exc), 401)
        except AuthorizationError as exc:
            return await self._page(visit, str(exc), 403)

        return RedirectResponse(
            self._server.signed_in(visit.held, signed), status_code=302
        )

    async def sso(self, request: Request) -> Response:
        query = request.query_params
        visit = self._visit(request, query.get(LoginField.TXN.value, ""))
        if visit is None:
            return self._expired()

        if not visit.confirmed(query.get(LoginField.PROOF.value, "")):
            return await self._page(visit, self.UNCONFIRMED, 403)

        try:
            outcome = await self._sign_ins.exchange().handshake(
                self._requests.of(request)
            )
        except AuthorizationError as exc:
            return await self._page(visit, str(exc), 403)

        if isinstance(outcome, SsoChallenge):
            # браузер домена повторит запрос с билетом сам; остальным страница
            # входа остаётся с причиной и формой
            return await self._page(
                visit, self.NO_TICKET, 401, self._responses.headers()
            )

        return RedirectResponse(
            self._server.signed_in(visit.held, outcome.signed), status_code=302
        )

    def _visit(self, request: Request, txn: str) -> LoginVisit | None:
        """Заход на страницу начатого входа; None — вход не начат или истёк."""
        held = self._server.pending(txn)
        if held is None:
            return None

        cookie = request.cookies.get(LoginField.COOKIE.value, "")

        return LoginVisit(txn=txn, held=held, cookie=cookie)

    async def _page(
        self,
        visit: LoginVisit,
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

        proof = visit.cookie
        if not proof:
            proof = secrets.token_urlsafe(32)

        form = ""
        if providers.password:
            form = self.FORM.format(
                action=html.escape(f"{self._public_url}{AuthServer.LOGIN_PATH}"),
                txn=html.escape(visit.txn),
                proof=html.escape(proof),
            )

        sso = ""
        if providers.sso:
            query = urlencode(
                {AuthServer.TXN: visit.txn, LoginField.PROOF.value: proof}
            )
            url = f"{self._public_url}{LoginField.SSO.value}?{query}"
            sso = self.SSO.format(url=html.escape(url))

        sent = dict(self.HEADERS)
        if headers is not None:
            sent.update(headers)

        response = HTMLResponse(
            self.PAGE.format(
                service=self._service,
                error=shown,
                client=await self._client(visit.held),
                form=form,
                sso=sso,
            ),
            status_code=status,
            headers=sent,
        )
        response.set_cookie(
            LoginField.COOKIE.value,
            proof,
            max_age=AuthServer.PENDING_SEC,
            path=self._cookie_path,
            secure=self._cookie_secure,
            httponly=True,
            samesite="strict",
        )

        return response

    async def _client(self, held: PendingLogin) -> str:
        """Кто просит вход и куда вернётся браузер: по этой строке человек
        решает, его ли это вход."""
        name: str | None = None
        client = await self._server.get_client(held.client_id)
        if client is not None:
            name = client.client_name

        if not name:
            name = self.UNNAMED

        return self.CLIENT.format(
            service=self._service,
            name=html.escape(name),
            redirect=html.escape(str(held.params.redirect_uri)),
        )

    def _expired(self) -> Response:
        text = (
            "the sign-in is not started or has expired: start it again from the client"
        )

        return HTMLResponse(
            self.PAGE.format(
                service=self._service,
                error=self.ERROR.format(text=text),
                client="",
                form="",
                sso="",
            ),
            status_code=400,
            headers=self.HEADERS,
        )


class ServiceAuth:
    """Вход службы целиком: сервер авторизации, страницы входа и проверяющие
    токена её ресурсов.

    Создаётся сборкой процесса службы (boba-mcp, служба агента) с её именем
    на странице входа и ресурсами по именам; сборка приложения берёт
    провайдер авторизации на каждый ресурс и маршруты приложения под
    префиксом службы (app_routes).
    """

    def __init__(
        self,
        public_url: str,
        server: AuthServer,
        tokens: ServiceTokens,
        service: str,
        resources: Mapping[str, str],
    ) -> None:
        self._public_url = public_url
        self._server = server
        self._pages = LoginPages(server, public_url, service)
        self._tokens = tokens
        self._resources = dict(resources)

    def published(self) -> str:
        """Префикс пути, под которым служба отвечает: путь публичного адреса
        (равен url_prefix секции службы, ServiceAddress); в корне — пустой."""
        return urlsplit(self._public_url).path.rstrip("/")

    def resource(self, name: str, scopes: Sequence[str]) -> RemoteAuthProvider:
        """Провайдер авторизации ресурса name: принимает только токен этого
        ресурса с областями scopes и объявляет сервер авторизации в
        метаданных ресурса (RFC 9728)."""
        return RemoteAuthProvider(
            token_verifier=ResourceTokens(self._tokens, self._resources[name], scopes),
            authorization_servers=[AnyHttpUrl(self._public_url)],
            base_url=self._public_url,
        )

    def app_routes(
        self, discovery: Sequence[BaseRoute], served: Sequence[BaseRoute]
    ) -> list[BaseRoute]:
        """Маршруты приложения службы под её префиксом.

        Метаданные (RFC 8414, RFC 9728) клиент ищет от корня хоста по пути с
        префиксом внутри — они стоят в корне: метаданные сервера авторизации и
        discovery ресурсов службы. Всё остальное — сервер авторизации, страницы
        входа и served службы — отвечает под префиксом публичного адреса:
        прокси передаёт путь как есть.
        """
        mounted: list[BaseRoute] = list(self._server.get_routes())
        mounted.extend(self._pages.routes())
        mounted.extend(served)

        routes: list[BaseRoute] = list(self._server.get_well_known_routes())
        routes.extend(discovery)
        routes.append(Mount(self.published(), routes=mounted))

        return routes
