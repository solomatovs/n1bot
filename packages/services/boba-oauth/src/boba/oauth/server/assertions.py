"""Выбор проверяющего OAuth-утверждения по настроенному realm."""

from __future__ import annotations

from collections.abc import Sequence

import jwt

from boba.auth.config import ProxyAuthConfig
from boba.oauth.assertions import (
    AssertionClaim,
    AssertionRejectedError,
    ProxyAssertion,
    ProxyAssertions,
)


class ProxyAssertionVerifier:
    """Проверка утверждений нескольких пространств по настроенным proxy-бэкендам.

    AuthServer выбирает ключ по публичному kid; личность и её пространство
    принимаются только после проверки подписи и соответствия kid телу JWT.
    """

    def __init__(self, configs: Sequence[ProxyAuthConfig]) -> None:
        self._assertions: dict[str, ProxyAssertions] = {}
        for config in configs:
            realm = config.identity.realm
            if realm in self._assertions:
                msg = f"proxy assertions: multiple backends for realm {realm!r}"
                raise ValueError(msg)

            self._assertions[realm] = ProxyAssertions(
                config.secret.get_secret_value(), config.max_skew_sec
            )

    @property
    def configured(self) -> bool:
        return bool(self._assertions)

    def read(self, assertion: str, client_id: str, audience: str) -> ProxyAssertion:
        try:
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError as exc:
            msg = f"identity assertion of client {client_id!r}: invalid JWT header"
            raise AssertionRejectedError(msg) from exc

        realm = header.get(AssertionClaim.KEY_ID.value)
        if not isinstance(realm, str):
            msg = f"identity assertion of client {client_id!r}: expected a realm in kid"
            raise AssertionRejectedError(msg)

        verifier = self._assertions.get(realm)
        if verifier is None:
            msg = (
                f"identity assertion of client {client_id!r}: "
                f"no proxy backend is configured for realm {realm!r}"
            )
            raise AssertionRejectedError(msg)

        return verifier.read(assertion, client_id, audience)
