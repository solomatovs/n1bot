"""Соединения пользователя: общие (по роли) видны, свои (личный грант) — правятся.

Владение = личный грант: строка, выданная пользователю лично, принадлежит ему —
он её создаёт, заменяет и удаляет. Выданная по роли — общая, только для чтения.

Проверка: POST /connections/check {profile} и POST /connections/{id}/check — пробное
соединение; исход всегда 200 с ProbeResult{ok, message, elapsed_ms}.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя; соединение общее, а не своё.
404 — соединение не видно пользователю.
409 — имя занято среди видимых пользователю соединений.
422 — в профиле замаскированный секрет из ответа GET вместо настоящего.
503 — секция [connections] выключена или хранилище недоступно.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
)

from boba.chat.profiles import ChatProfiles
from boba.connection_broker.probe import ConnectionProbe, ProbeResult
from boba.connection_broker.store import ConnectionStore, ConnectionStoreError
from boba.connection_broker.user_connections import StoreRef, TicketsRef, UserKerberos
from boba.connections.profile import (
    ConnectionKind,
    ConnectionProfile,
    StoredConnection,
)
from boba.identity.api import ApiSubject
from boba.identity.context import Scope, Subject
from boba.identity.locks import LockToken
from boba.krb import KerberosCredentials
from boba.messaging import ChangeAction, ConnectionsChanged, MessageBus
from boba.studio.api.auth import ApiIdentity, CurrentUser
from boba.studio.api.urls import ConnectionUrl

__all__ = [
    "ConnectionBody",
    "ConnectionDeleted",
    "ConnectionView",
    "ConnectionsApi",
    "ProbeBody",
    "ProfileSchema",
]


class MaskedSecrets:
    """Замаскированные SecretStr из ответа GET: назад в профиль их принимать нельзя."""

    MASK: ClassVar[str] = "**********"

    @classmethod
    def find(cls, value: Any) -> bool:
        if isinstance(value, SecretStr):
            return value.get_secret_value() == cls.MASK

        if isinstance(value, BaseModel):
            for field in value.__dict__.values():
                if cls.find(field):
                    return True

        return False


BusSource = Callable[[], MessageBus]
"""Шина процесса; зовётся на запрос."""


class ConnectionBody(BaseModel):
    """Имя и рабочий профиль соединения с настоящими секретами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    profile: ConnectionProfile

    @field_validator("profile")
    @classmethod
    def _real_secrets(cls, value: ConnectionProfile) -> ConnectionProfile:
        if MaskedSecrets.find(value):
            msg = "profile carries a masked secret: enter the real value"
            raise ValueError(msg)

        return value


class ConnectionView(BaseModel):
    """Строка connections для страницы: профиль с замаскированными секретами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    name: str
    kind: ConnectionKind
    mine: bool
    profile: ConnectionProfile

    @classmethod
    def of(cls, row: StoredConnection, mine: bool) -> ConnectionView:
        return cls(
            id=row.id, name=row.name, kind=row.kind, mine=mine, profile=row.profile
        )


class ProbeBody(BaseModel):
    """Профиль на проверку: как в форме, ещё не сохранённый."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: ConnectionProfile

    @field_validator("profile")
    @classmethod
    def _real_secrets(cls, value: ConnectionProfile) -> ConnectionProfile:
        if MaskedSecrets.find(value):
            msg = "profile carries a masked secret: enter the real value"
            raise ValueError(msg)

        return value


class ConnectionDeleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted: bool


class ProfileSchema:
    """JSON Schema профиля соединения: по ней страница строит форму."""

    _ADAPTER: ClassVar[TypeAdapter[ConnectionProfile]] = TypeAdapter(ConnectionProfile)

    @classmethod
    def render(cls) -> Mapping[str, Any]:
        return cls._ADAPTER.json_schema()


class ConnectionsApi:
    """Обработчики /connections."""

    TAG: ClassVar[str] = "connections"

    def __init__(
        self,
        store: StoreRef,
        profiles: ChatProfiles,
        tickets: TicketsRef,
        bus: BusSource,
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._tickets = tickets
        self._bus = bus

    async def _changed(
        self, subject: Subject, connection_id: UUID, name: str, action: ChangeAction
    ) -> None:
        """Сообщает ленте соединений пользователя на всех инстансах об изменении."""
        message = ConnectionsChanged(
            connection_id=connection_id, name=name, action=action
        )
        await self._bus().publish(
            Scope.user(subject.user_id), message, LockToken.local()
        )

    def mount(self, router: APIRouter) -> None:
        routes = (
            (ConnectionUrl.SCHEMA, self.schema, "GET"),
            (ConnectionUrl.CONNECTIONS, self.list_connections, "GET"),
            (ConnectionUrl.CONNECTIONS, self.create, "POST"),
            (ConnectionUrl.CHECK, self.check, "POST"),
            (ConnectionUrl.CONNECTION_CHECK, self.check_stored, "POST"),
            (ConnectionUrl.CONNECTION, self.replace, "PUT"),
            (ConnectionUrl.CONNECTION, self.delete, "DELETE"),
        )
        for path, handler, method in routes:
            router.add_api_route(path.value, handler, methods=[method], tags=[self.TAG])

    async def schema(self, current_user: CurrentUser) -> Mapping[str, Any]:
        """Схема профиля с вариантами по kind и method; секреты — format=password."""
        ApiIdentity.user_of(current_user)

        return ProfileSchema.render()

    async def list_connections(
        self,
        current_user: CurrentUser,
        kind: ConnectionKind | None = None,
        profile: str | None = None,
    ) -> Sequence[ConnectionView]:
        subject = self._subject(current_user, profile)
        store = self._resolved()

        kinds: list[ConnectionKind] = list(ConnectionKind)
        if kind is not None:
            kinds = [kind]

        rows = await self._visible(store, subject, kinds)
        owned = await self._owned(store, subject)

        views: list[ConnectionView] = []
        for row in rows:
            views.append(ConnectionView.of(row, row.id in owned))

        return views

    async def create(
        self,
        body: ConnectionBody,
        current_user: CurrentUser,
        profile: str | None = None,
    ) -> ConnectionView:
        subject = self._subject(current_user, profile)
        store = self._resolved()

        await self._require_free_name(store, subject, body.name, except_id=None)

        try:
            connection_id = await store.add_owned(
                body.name, body.profile, subject.user_id
            )
            row = await store.get(connection_id)
        except ConnectionStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        await self._changed(subject, connection_id, body.name, ChangeAction.CREATED)
        return ConnectionView.of(row, mine=True)

    async def replace(
        self,
        connection_id: UUID,
        body: ConnectionBody,
        current_user: CurrentUser,
        profile: str | None = None,
    ) -> ConnectionView:
        subject = self._subject(current_user, profile)
        store = self._resolved()

        await self._require_owned(store, subject, connection_id)
        await self._require_free_name(
            store, subject, body.name, except_id=connection_id
        )

        try:
            await store.update(connection_id, body.name, body.profile)
            row = await store.get(connection_id)
        except ConnectionStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        await self._changed(subject, connection_id, body.name, ChangeAction.UPDATED)
        return ConnectionView.of(row, mine=True)

    async def delete(
        self, connection_id: UUID, current_user: CurrentUser, profile: str | None = None
    ) -> ConnectionDeleted:
        subject = self._subject(current_user, profile)
        store = self._resolved()

        await self._require_owned(store, subject, connection_id)

        try:
            row = await store.get(connection_id)
            deleted = await store.remove(connection_id)
        except ConnectionStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if deleted:
            await self._changed(subject, connection_id, row.name, ChangeAction.DELETED)

        return ConnectionDeleted(deleted=deleted)

    async def check(
        self, body: ProbeBody, current_user: CurrentUser, profile: str | None = None
    ) -> ProbeResult:
        """Пробное соединение по профилю из формы; делегирование — билетом входа."""
        identity = ApiIdentity.resolve(current_user, profile, self._profiles)

        return await self._probe(identity).probe(body.profile)

    async def check_stored(
        self, connection_id: UUID, current_user: CurrentUser, profile: str | None = None
    ) -> ProbeResult:
        """Пробное соединение по сохранённой строке: видимой пользователю."""
        identity = ApiIdentity.resolve(current_user, profile, self._profiles)
        store = self._resolved()

        visible = await self._visible(store, identity.subject, list(ConnectionKind))
        for row in visible:
            if row.id == connection_id:
                return await self._probe(identity).probe(row.profile)

        raise HTTPException(
            status_code=404, detail=f"connection #{connection_id} not found"
        )

    def _probe(self, identity: ApiSubject) -> ConnectionProbe:
        def delegation() -> KerberosCredentials:
            ticket = UserKerberos.ticket_of(identity.credential, identity.subject.login)
            return UserKerberos.open_credentials(ticket, self._tickets())

        return ConnectionProbe(delegation)

    def _subject(self, current_user: CurrentUser, profile: str | None) -> Subject:
        return ApiIdentity.resolve(current_user, profile, self._profiles).subject

    def _resolved(self) -> ConnectionStore:
        try:
            return self._store()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @staticmethod
    async def _visible(
        store: ConnectionStore, subject: Subject, kinds: Sequence[ConnectionKind]
    ) -> list[StoredConnection]:
        """Свои и выданные по роли соединения указанных видов."""
        rows: list[StoredConnection] = []
        try:
            for kind in kinds:
                rows.extend(await store.for_subject(subject, kind))
        except ConnectionStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return rows

    @staticmethod
    async def _owned(store: ConnectionStore, subject: Subject) -> frozenset[UUID]:
        try:
            return await store.owned_ids(subject.user_id)
        except ConnectionStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def _require_owned(
        self, store: ConnectionStore, subject: Subject, connection_id: UUID
    ) -> None:
        """404 — не видно пользователю, 403 — видно, но общее."""
        owned = await self._owned(store, subject)
        if connection_id in owned:
            return

        visible = await self._visible(store, subject, list(ConnectionKind))
        for row in visible:
            if row.id == connection_id:
                msg = f"connection #{connection_id} is shared: only its owner edits it"
                raise HTTPException(status_code=403, detail=msg)

        raise HTTPException(
            status_code=404, detail=f"connection #{connection_id} not found"
        )

    async def _require_free_name(
        self,
        store: ConnectionStore,
        subject: Subject,
        name: str,
        except_id: UUID | None,
    ) -> None:
        """Имя уникально среди видимых: инструменты выбирают соединение по имени."""
        visible = await self._visible(store, subject, list(ConnectionKind))
        for row in visible:
            if row.id == except_id:
                continue

            if row.name == name:
                msg = f"connection name {name!r} is already used"
                raise HTTPException(status_code=409, detail=msg)
