"""Проверка OAuth-утверждений настроенным proxy-бэкендом."""

from collections.abc import Sequence

from boba.auth.config import ProxyAuthConfig
from boba.oauth.assertions import (
    AssertionRejectedError,
    ProxyAssertion,
    ProxyAssertions,
)


class ProxyAssertionVerifier:
    """Ключ proxy подтверждает утверждение аутентифицированного OAuth-клиента."""

    def __init__(self, configs: Sequence[ProxyAuthConfig]) -> None:
        if len(configs) > 1:
            raise ValueError("proxy assertions: multiple backends configured")
        self._assertions = (
            ProxyAssertions(
                configs[0].secret.get_secret_value(), configs[0].max_skew_sec
            )
            if configs
            else None
        )

    @property
    def configured(self) -> bool:
        return self._assertions is not None

    def read(self, assertion: str, client_id: str, audience: str) -> ProxyAssertion:
        if self._assertions is None:
            raise AssertionRejectedError("no proxy backend is configured")
        return self._assertions.read(assertion, client_id, audience)
