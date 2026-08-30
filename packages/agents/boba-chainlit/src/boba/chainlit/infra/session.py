"""Сессия chainlit: реализация доменного контракта и вход к её данным.

Здесь единственное место, знающее, как chainlit хранит сессию: контекст
вызова, реестр сокетов, user_session и JWT входа. Логика приложения
работает с протоколом Session, инфраструктура — с этим классом; контекст
вызова для хода чата собирается тоже здесь.

Ошибки:
AuthenticationError — у сессии нет токена входа либо он не принят (истёк,
    чужая подпись, порча): ход отказывается, пользователю нужен новый вход.
InternalServiceError — контекст вызова не собрать: у сессии нет треда,
    сохранённого пользователя или выбранного профиля.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import chainlit as cl
from boba.cancellation import RunCancellation
from boba.chainlit.domain.context import ChatCallContext
from boba.identity.context import (
    ChatInitiator,
    Credential,
    DelegatedTicket,
    Scope,
    Subject,
)
from boba.identity.errors import AuthenticationError, InternalServiceError
from boba.identity.session import Session, SessionSource
from boba.identity.signin import SignInMetadata
from boba.identity.token import (
    SessionClaims,
    TokenReader,
    TokenRejectedError,
    TokenRejection,
)
from boba.runtime.refresh import LiveSessions, LiveToken
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

    def __init__(self, session: Any | None, tokens: TokenReader) -> None:
        self._session = session
        self._tokens = tokens

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
        """Роли входа — из токена сессии: их выдал вход, строка users их не хранит."""
        return self.signed_in().sign_in().roles

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

    def signed_in(self) -> SessionClaims:
        """Вход, каким его выпустил токен сессии; без годного токена — отказ.

        Chainlit проверяет токен один раз при подключении сокета, поэтому
        истёкший срок замечает именно это место — на каждом ходе.
        """
        token = self.token
        if not token:
            msg = "this session carries no sign-in token: reload the page to sign in"
            raise AuthenticationError(msg)

        try:
            return self._tokens.read(token)
        except TokenRejectedError as exc:
            if exc.reason is TokenRejection.EXPIRED:
                msg = "your sign-in has expired: reload the page to sign in again"
                raise AuthenticationError(msg) from exc

            msg = (
                f"sign-in token rejected ({exc.reason}): "
                "reload the page to sign in again"
            )
            raise AuthenticationError(msg) from exc

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

        subject = self._subject(profile)

        return ChatCallContext(
            subject=subject,
            scope=Scope.chat(thread_id),
            initiator=ChatInitiator(thread_id=thread_id, turn_id=turn_id),
            credential=self._credential(),
            cancellation=RunCancellation(),
            surface=self,
        )

    def _subject(self, profile: str) -> Subject:
        """Субъект по строке users; без сохранённой строки контекста нет."""
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
            parsed = UUID(user_id)
        except ValueError as exc:
            raise InternalServiceError(
                internal_detail=f"user id {user_id!r} is not the users.id uuid",
                user_detail=None,
            ) from exc

        return Subject.of_user(parsed, self.label, self.roles, profile)

    def _credential(self) -> Credential:
        """Ссылка на делегированный билет входа по JWT сессии; иначе — причина."""
        return self.signed_in().sign_in().credential()

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
        return SignInMetadata.parse(cls.metadata_of(user)).roles


class ChainlitSessions(SessionSource, LiveSessions):
    """Источник сессий chainlit: контекст вызова, реестр сокетов и тред."""

    _installed: ClassVar[ChainlitSessions | None] = None

    def __init__(self, tokens: TokenReader) -> None:
        self._tokens = tokens

    def ticket_of_token(self, token: str) -> DelegatedTicket | None:
        """Ссылка на билет входа из JWT; None — токен негоден или вход не SSO."""
        try:
            claims = self._tokens.read(token)
        except TokenRejectedError as exc:
            logger.info("sign-in token rejected: %s", exc.reason)
            return None

        return claims.ticket()

    @classmethod
    def install(cls, source: ChainlitSessions) -> None:
        cls._installed = source

    @classmethod
    def installed(cls) -> ChainlitSessions:
        """Отсутствие источника — ошибка сборки: позвали раньше bootstrap."""
        if cls._installed is None:
            msg = "session source is not installed: bootstrap has not run"
            raise RuntimeError(msg)

        return cls._installed

    def current(self) -> ChainlitSession:
        """Сессия текущего вызова; вне контекста chainlit — пустая обёртка."""
        try:
            return ChainlitSession(cl.context.session, self._tokens)
        except ChainlitContextException:
            return ChainlitSession(None, self._tokens)

    def of(self, session: Any | None) -> ChainlitSession:
        """Обёртка вокруг готового объекта сессии."""
        return ChainlitSession(session, self._tokens)

    def by_id(self, session_id: str) -> ChainlitSession:
        """Сессия по её id: так её находит http-обработчик вложений."""
        if not session_id:
            return ChainlitSession(None, self._tokens)

        return ChainlitSession(WebsocketSession.get_by_id(session_id), self._tokens)

    def of_socket(self, sid: str) -> ChainlitSession:
        """Сессия по socket-id: так её находит обработчик события сокета."""
        return ChainlitSession(WebsocketSession.get(sid), self._tokens)

    def adopt_token(self, identifier: str, token: str) -> int:
        """Живые сокет-сессии пользователя получают новый JWT; итог — сколько."""
        adopted = 0
        for session in list(ws_sessions_id.values()):
            user = getattr(session, "user", None)
            if user is None:
                continue

            if user.identifier != identifier:
                continue

            session.token = token
            adopted += 1

        return adopted

    def live_tokens(self) -> list[LiveToken]:
        """Токены живых сокет-сессий с сохранённым пользователем: для сторожа сессий."""
        found: list[LiveToken] = []
        for session in list(ws_sessions_id.values()):
            wrapped = ChainlitSession(session, self._tokens)
            user_id = wrapped.user_id
            if not user_id:
                continue

            if not wrapped.token:
                continue

            try:
                parsed = UUID(user_id)
            except ValueError:
                continue

            found.append(
                LiveToken(user_id=parsed, login=wrapped.label, token=wrapped.token)
            )

        return found

    def of_user(self, user_id: UUID) -> list[ChainlitSession]:
        """Живые сессии пользователя на этом инстансе: все его вкладки."""
        found: list[ChainlitSession] = []
        for session in list(ws_sessions_id.values()):
            user = getattr(session, "user", None)
            if user is None:
                continue

            if str(getattr(user, "id", "")) != str(user_id):
                continue

            found.append(ChainlitSession(session, self._tokens))

        return found

    def in_thread(self, thread_id: str) -> list[ChainlitSession]:
        """Живые сессии, открывшие этот тред: у треда бывает много вкладок."""
        found: list[ChainlitSession] = []
        for session in list(ws_sessions_id.values()):
            if session.thread_id != thread_id:
                continue

            found.append(ChainlitSession(session, self._tokens))

        return found


def session_source_ref() -> ChainlitSessions:
    """Источник сессий для мест вне DI-графа: ставит bootstrap на старте."""
    return ChainlitSessions.installed()


def current_session() -> ChainlitSession:
    """Сессия текущего вызова: короткий вход для мест вне DI-графа."""
    return session_source_ref().current()
