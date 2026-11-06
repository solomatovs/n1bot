"""Доверенные и динамически зарегистрированные клиенты сервера OAuth."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from mcp.shared.auth import JWT_BEARER_GRANT_TYPE, OAuthClientInformationFull
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.oauth.server.sealed import SealedValues


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
        self, sealed: SealedValues, declared: Mapping[str, TrustedClient]
    ) -> None:
        self._sealed = sealed
        self._declared: dict[str, OAuthClientInformationFull] = {}
        for client_id, config in declared.items():
            self._declared[client_id] = config.registered(client_id)

    def permits(self, client_id: str) -> bool:
        return client_id in self._declared

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
