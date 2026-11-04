"""Подпись и проверка утверждения клиента OAuth о личности пользователя."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import ClassVar
from uuid import uuid4

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from boba.identity.principal import UserIdentity


class AssertionRejectedError(Exception):
    """Утверждение доверенного бэкенда не принято."""


class AssertionClaim(StrEnum):
    """Имена клеймов и заголовка утверждения (RFC 7523, SEP-990)."""

    TYPE = "oauth-id-jag+jwt"
    ALGORITHM = "HS256"
    ROLES = "roles"
    IDENTITY = "identity"
    KEY_ID = "kid"


class ProxyAssertion(BaseModel):
    """Утверждение доверенного бэкенда о пользователе: кто выпустил (iss и
    client_id — идентификатор клиента OAuth), о ком (sub — логин), для какого
    сервера авторизации (aud) и ресурса (resource), с какими ролями пользователь
    вошёл в бэкенд. Живёт секунды: iat..exp; jti — против повтора."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    iss: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    aud: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    roles: str = ""
    identity: UserIdentity
    jti: str = Field(min_length=1)
    iat: int
    exp: int

    @model_validator(mode="after")
    def _same_subject(self) -> ProxyAssertion:
        if not self.identity.matches(self.sub):
            msg = (
                f"identity assertion subject {self.sub!r}: expected "
                f"{self.identity.identifier!r} from identity"
            )
            raise ValueError(msg)

        return self


class ProxyAssertions:
    """Выпуск и проверка утверждения общим секретом [auth.proxy].secret.

    Выпускает клиент (чат) на каждый обмен, проверяет сервер авторизации
    сервиса boba-mcp в exchange_identity_assertion. Памяти предъявленных
    утверждений нет: живёт оно TTL_SEC и без секрета клиента бесполезно.
    """

    TTL_SEC: ClassVar[int] = 60

    def __init__(self, secret: str, max_skew_sec: int) -> None:
        self._secret = secret
        self._max_skew_sec = max_skew_sec

    def issue(
        self, client_id: str, login: str, roles: str, audience: str, resource: str
    ) -> str:
        now = int(time.time())
        identity = UserIdentity.model_validate(login)
        claims = ProxyAssertion(
            iss=client_id,
            sub=login,
            aud=audience,
            client_id=client_id,
            resource=resource,
            roles=roles,
            identity=identity,
            jti=uuid4().hex,
            iat=now,
            exp=now + self.TTL_SEC,
        )

        return jwt.encode(
            claims.model_dump(mode="json"),
            self._secret,
            algorithm=AssertionClaim.ALGORITHM.value,
            headers={
                "typ": AssertionClaim.TYPE.value,
                AssertionClaim.KEY_ID.value: identity.realm,
            },
        )

    def read(self, assertion: str, client_id: str, audience: str) -> ProxyAssertion:
        """Утверждение клиента client_id для сервера audience; иначе —
        AssertionRejectedError с причиной."""
        # издатель в метаданных и у клиента может отличаться завершающим слэшем
        issuer = audience.rstrip("/")
        try:
            header = jwt.get_unverified_header(assertion)
            raw = jwt.decode(
                assertion,
                self._secret,
                algorithms=[AssertionClaim.ALGORITHM.value],
                audience=[issuer, f"{issuer}/"],
                issuer=client_id,
                leeway=self._max_skew_sec,
            )
            claims = ProxyAssertion.model_validate(raw)
        except (jwt.PyJWTError, ValidationError) as exc:
            msg = (
                f"identity assertion of client {client_id!r} for {audience}: "
                f"expected an HS256 JWT under [auth.proxy].secret, got {exc}"
            )
            raise AssertionRejectedError(msg) from exc

        if header.get("typ") != AssertionClaim.TYPE.value:
            msg = (
                f"identity assertion of client {client_id!r}: header typ expects "
                f"{AssertionClaim.TYPE.value!r}, got {header.get('typ')!r}"
            )
            raise AssertionRejectedError(msg)

        if header.get(AssertionClaim.KEY_ID.value) != claims.identity.realm:
            msg = (
                f"identity assertion of {claims.sub!r}: key id does not "
                "match the verified identity realm"
            )
            raise AssertionRejectedError(msg)

        if claims.client_id != client_id:
            msg = (
                f"identity assertion of {claims.sub!r}: issued to client "
                f"{claims.client_id!r}, presented by client {client_id!r}"
            )
            raise AssertionRejectedError(msg)

        if claims.exp - claims.iat > self.TTL_SEC:
            msg = (
                f"identity assertion of {claims.sub!r}: lifetime "
                f"{claims.exp - claims.iat}s exceeds {self.TTL_SEC}s"
            )
            raise AssertionRejectedError(msg)

        return claims
