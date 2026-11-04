"""Адаптер личности между AuthService и моделями пользователя Chainlit."""

from datetime import datetime
from uuid import UUID

import chainlit as cl

from boba.identity.api import AuthenticatedUser
from boba.identity.errors import AuthenticationError
from boba.identity.session import Login, UserMetadataField
from boba.identity.signin import SignInMetadata


class ChainlitPrincipal:
    """Единственная граница преобразования principal в формат Chainlit."""

    @staticmethod
    def project(
        principal: AuthenticatedUser,
        display_name: str = "",
    ) -> cl.PersistedUser:
        metadata = principal.sign_in.render()
        metadata[UserMetadataField.ROLES] = sorted(principal.roles)
        if not principal.roles:
            metadata[UserMetadataField.PROFILES] = []
            metadata[UserMetadataField.PROFILE] = ""

        return cl.PersistedUser(
            id=str(principal.id),
            identifier=principal.identifier,
            display_name=display_name,
            metadata=metadata,
            createdAt=principal.created_at.isoformat(),
        )

    @staticmethod
    def restore(user: cl.User | cl.PersistedUser | None) -> AuthenticatedUser | None:
        """Восстанавливает principal из projection, выданной нашим auth callback."""
        if user is None or not isinstance(user, cl.PersistedUser):
            return None

        try:
            identifier = Login(user.identifier)
            sign_in = SignInMetadata.parse(user.metadata or {})
            sign_in.identity_for(identifier)
            return AuthenticatedUser(
                id=UUID(user.id),
                identifier=identifier,
                created_at=datetime.fromisoformat(user.createdAt),
                sign_in=sign_in,
            )
        except (TypeError, ValueError) as exc:
            raise AuthenticationError(
                "Chainlit user projection is not a valid authenticated principal"
            ) from exc
