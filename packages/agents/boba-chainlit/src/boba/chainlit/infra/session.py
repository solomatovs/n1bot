"""Сессия chainlit: реализация доменного контракта и вход к её данным.

Здесь единственное место, знающее, как chainlit хранит сессию: контекст
вызова, реестр сокетов, user_session и JWT входа. Логика приложения
работает с протоколом Session, инфраструктура — с этим классом; контекст
вызова для хода чата собирается тоже здесь.

Ошибки:
InternalServiceError — контекст вызова не собрать: у сессии нет треда,
    сохранённого пользователя или выбранного профиля.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import jwt

import chainlit as cl
from boba.cancellation import RunCancellation
from boba.chainlit.domain.context import (
    ChatCallContext,
    ChatInitiator,
    Credential,
    DelegatedTicket,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.chainlit.domain.errors import InternalServiceError
from boba.chainlit.domain.session import (
    Session,
    SsoMarks,
    UserMetadataField,
)
from chainlit.auth.jwt import decode_jwt
from chainlit.config import config as chainlit_config
from chainlit.context import ChainlitContextException
from chainlit.session import WebsocketSession, ws_sessions_id

__all__ = [
    "ChainlitSession",
    "ChainlitSessions",
    "current_session",
    "session_source_ref",
]

logger = logging.getLogger(__name__)


class ChainlitSession(Session):
    """Сессия chainlit: доменный контракт плюс то, что нужно инфраструктуре.

    Оборачивает объект сессии, а не контекст: одна и та же обёртка годится
    и для сессии текущего вызова, и для чужой, найденной по id. Пустая
    обёртка отвечает «ничего», а не падает: решение, что делать без
    сессии, принимает вызывающий.
    """

    def __init__(self, session: Any | None) -> None:
        self._session = session

    @property
    def present(self) -> bool:
        return self._session is not None

    @property
    def raw(self) -> Any | None:
        """Сам объект сессии: нужен чужому API chainlit, не логике."""
        return self._session

    @property
    def id(self) -> str:
        """Идентификатор сессии; пустая строка — сессии нет."""
        return str(getattr(self._session, "id", ""))

    @property
    def token(self) -> str:
        """JWT входа; пустая строка — сессии или токена нет."""
        return str(getattr(self._session, "token", "") or "")

    @property
    def thread_id(self) -> str | None:
        return getattr(self._session, "thread_id", None)

    @property
    def restored(self) -> bool:
        """Сессия поднята реконнектом, а не создана заново."""
        return bool(getattr(self._session, "restored", False))

    @property
    def socket_id(self) -> str:
        """Сокет сессии; пустая строка — сессия не сокетная."""
        return str(getattr(self._session, "socket_id", "") or "")

    @property
    def websocket(self) -> WebsocketSession | None:
        """Сокетная сессия: с ней работают рассылка хода и эмиттеры."""
        if not isinstance(self._session, WebsocketSession):
            return None

        return self._session

    @property
    def user(self) -> cl.User | cl.PersistedUser | None:
        """Строка users, слитая всеми входами подряд.

        Metadata здесь живут дольше одного входа; ровно этот вход
        описывает login_user().
        """
        return getattr(self._session, "user", None)

    @property
    def user_id(self) -> str | None:
        return getattr(self.user, "id", None)

    @property
    def identifier(self) -> str:
        return str(getattr(self.user, "identifier", "") or "")

    @property
    def label(self) -> str:
        identifier = self.identifier
        if identifier:
            return identifier

        return str(getattr(self.user, "id", "") or "")

    @property
    def metadata(self) -> Mapping[str, object]:
        return self.metadata_of(self.user)

    @property
    def roles(self) -> frozenset[str]:
        return self.roles_of(self.user)

    @property
    def chat_profile(self) -> str | None:
        value = getattr(self._session, "chat_profile", None)
        if not value:
            return None

        return str(value)

    @property
    def language(self) -> str:
        """Язык интерфейса: навязанный конфигом chainlit либо язык вкладки.

        Тот же порядок, что у самого chainlit (config.ui.language or
        language): иначе панель говорила бы не на языке интерфейса.
        """
        if forced := chainlit_config.ui.language:
            return forced

        if not isinstance(self._session, WebsocketSession):
            return ""

        return self._session.language

    @property
    def files(self) -> dict[str, Any]:
        """Файлы, которые chainlit держит на сессии."""
        return getattr(self._session, "files", {})

    @property
    def files_dir(self) -> Path:
        return Path(getattr(self._session, "files_dir", ""))

    def file_spec(self, parent_id: str | None) -> Any:
        """Спека файла, которого ждёт сообщение-родитель; None — не ждёт."""
        if parent_id is None:
            return None

        specs: dict[str, Any] = getattr(self._session, "files_spec", {})
        return specs.get(parent_id)

    def login_user(self) -> cl.User | None:
        """Пользователь, каким его выпустил вход: из подписанного JWT сессии.

        None — сессии нет, токена нет или он не проходит проверку подписи
        и срока.
        """
        return self.user_of_token(self.token)

    def value(self, key: str, default: Any = None) -> Any:
        """Значение, положенное на сессию приложением (DI-контейнер и т.п.)."""
        if not self.present:
            return default

        return cl.user_session.get(key, default)

    def remember(self, key: str, value: Any) -> None:
        """Кладёт значение на сессию; вне сессии класть некуда."""
        if not self.present:
            return

        cl.user_session.set(key, value)

    async def emit(self, event: str, payload: Mapping[str, Any]) -> bool:
        """Шлёт событие в сокет сессии; False — слушать некому."""
        socket = self.websocket
        if socket is None:
            return False

        await cast("Awaitable[None]", socket.emit(event, dict(payload)))
        return True

    def call_context(self, turn_id: str, profile: str) -> ChatCallContext:
        """Контекст хода чата из сессии; чего не хватает — InternalServiceError.

        profile — имя профиля, по которому собран агент сессии: реестр
        профилей резолвит его по ролям, сессия хранит лишь сырой выбор.
        """
        thread_id = self.thread_id
        if not thread_id:
            raise InternalServiceError(
                internal_detail="call context needs a chainlit thread",
                user_detail=None,
            )

        subject = Subject(
            user_id=self._numeric_user_id(),
            login=self.label,
            roles=self.roles,
            profile=profile,
        )

        return ChatCallContext(
            subject=subject,
            scope=Scope.chat(thread_id),
            initiator=ChatInitiator(thread_id=thread_id, turn_id=turn_id),
            credential=self._credential(),
            cancellation=RunCancellation(),
            surface=self,
        )

    def _numeric_user_id(self) -> int:
        """id строки users; вход без сохранённой строки контекста не получает."""
        user_id = self.user_id
        if not user_id:
            raise InternalServiceError(
                internal_detail=(
                    f"call context needs a persisted user: sign-in {self.label!r} "
                    "has no users row"
                ),
                user_detail=None,
            )

        try:
            return int(user_id)
        except ValueError as exc:
            raise InternalServiceError(
                internal_detail=f"user id {user_id!r} is not the users.id integer",
                user_detail=None,
            ) from exc

    def _credential(self) -> Credential:
        """Ссылка на делегированный билет входа по JWT сессии; иначе — причина."""
        user = self.login_user()
        if user is None:
            return NoUserCredential(reason="this session has no signed sign-in")

        marks = SsoMarks.of_metadata(user.metadata)
        if marks is not None:
            return DelegatedTicket(principal=marks.principal, sso_login=marks.login)

        return NoUserCredential(reason=SsoMarks.absence_reason(user.metadata))

    @staticmethod
    def user_of_token(token: str) -> cl.User | None:
        """Пользователь из подписанного JWT; None — токен негоден."""
        if not token:
            return None

        try:
            return decode_jwt(token)
        except jwt.PyJWTError:
            return None

    @classmethod
    def marks_of_token(cls, token: str) -> SsoMarks | None:
        """Метки SSO-входа из JWT-cookie; None — токен негоден или вход не SSO."""
        user = cls.user_of_token(token)
        if user is None:
            return None

        return SsoMarks.of_metadata(user.metadata)

    @staticmethod
    def metadata_of(user: cl.User | cl.PersistedUser | None) -> Mapping[str, object]:
        """Metadata пользователя; у входа без metadata — пусто."""
        if user is None:
            return {}

        metadata = user.metadata
        if metadata is None:
            return {}

        return metadata

    @classmethod
    def roles_of(cls, user: cl.User | cl.PersistedUser | None) -> frozenset[str]:
        """Роли пользователя из metadata; годится и вне контекста сессии."""
        roles = cls.metadata_of(user).get(UserMetadataField.ROLES)
        if not roles:
            return frozenset()

        if isinstance(roles, str):
            return frozenset({roles})

        if not isinstance(roles, Iterable):
            return frozenset()

        names: set[str] = set()
        for role in roles:
            names.add(str(role))

        return frozenset(names)


class ChainlitSessions:
    """Источник сессий chainlit: контекст вызова, реестр сокетов и тред."""

    def current(self) -> ChainlitSession:
        """Сессия текущего вызова; вне контекста chainlit — пустая обёртка."""
        try:
            return ChainlitSession(cl.context.session)
        except ChainlitContextException:
            return ChainlitSession(None)

    def of(self, session: Any | None) -> ChainlitSession:
        """Обёртка вокруг готового объекта сессии."""
        return ChainlitSession(session)

    def by_id(self, session_id: str) -> ChainlitSession:
        """Сессия по её id: так её находит http-обработчик вложений."""
        if not session_id:
            return ChainlitSession(None)

        return ChainlitSession(WebsocketSession.get_by_id(session_id))

    def of_socket(self, sid: str) -> ChainlitSession:
        """Сессия по socket-id: так её находит обработчик события сокета."""
        return ChainlitSession(WebsocketSession.get(sid))

    def in_thread(self, thread_id: str) -> list[ChainlitSession]:
        """Живые сессии, открывшие этот тред: у треда бывает много вкладок."""
        found: list[ChainlitSession] = []
        for session in list(ws_sessions_id.values()):
            if session.thread_id != thread_id:
                continue

            found.append(ChainlitSession(session))

        return found


def session_source_ref() -> ChainlitSessions:
    """Источник сессий для мест вне DI-графа: тела инструментов, журнал.

    Контейнер поднят раньше их всех, поэтому источник берётся из корня, а
    не прокидывается через десяток фабрик. Провайдер импортируется лениво:
    providers собирает реестр инструментов, который сам зовёт эту функцию.

    Отсутствие корня — ошибка сборки, а не режим работы: значит функцию
    позвали там, где контейнера ещё нет.
    """
    from boba.chainlit.infra.di import Container  # noqa: PLC0415
    from boba.chainlit.infra.providers import session_source  # noqa: PLC0415

    root = Container.root
    if root is None:
        msg = "DI container is not initialised: session source is unavailable"
        raise RuntimeError(msg)

    source = root.resolved(session_source)
    if not isinstance(source, ChainlitSessions):
        msg = f"session source is {type(source).__name__}, expected ChainlitSessions"
        raise RuntimeError(msg)

    return source


def current_session() -> ChainlitSession:
    """Сессия текущего вызова: короткий вход для мест вне DI-графа."""
    return session_source_ref().current()
