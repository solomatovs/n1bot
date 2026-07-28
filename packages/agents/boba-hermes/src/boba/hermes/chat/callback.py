import logging
from typing import Annotated, ClassVar
from uuid import UUID

import chainlit as cl
from chainlit.data.base import BaseDataLayer
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.types import ThreadDict
from chainlit.user_session import UserSession
from fastapi import Request, Response

from boba.hermes.agent.api import HermesApi
from boba.hermes.chat.data import HermesProfileRepository
from boba.hermes.chat.handler import chainlit_error_ctx_handler
from boba.hermes.chat.turn import HermesTurn
from boba.hermes.infra.di import Depends, di_inject
from boba.hermes.infra.providers import (
    hermes_api,
    hermes_data_layer,
    hermes_profile_repository,
)

logger = logging.getLogger(__name__)


class ChainlitAdapter:
    "Мост взаимодействия бизнес логики и chainlit"

    # профиль hermes текущего пользователя; кладётся при входе в чат
    PROFILE_KEY: ClassVar[str] = "hermes_profile"

    @staticmethod
    def get_chat_id(session: HTTPSession | WebsocketSession):
        "возвращает идентификатор чата из сессии chainlit"
        return session.thread_id

    @staticmethod
    def get_user_id(session: UserSession) -> UUID:
        "uuid пользователя из data layer; без него профиль не к чему привязать"
        user = ChainlitAdapter.get_chat_user(session)
        if not isinstance(user, cl.PersistedUser):
            raise RuntimeError(f"user in session is not persisted: {type(user)}")

        return UUID(user.id)

    @staticmethod
    def get_chat_user(session: UserSession):
        "возвращает объект описывающий пользователя чата"
        if user := session.get("user"):
            if not isinstance(user, (cl.PersistedUser, cl.User)):
                raise RuntimeError(f"user in user_session is not valud: {type(user)}")

            return user

        raise RuntimeError("user does't exists in session")

    @staticmethod
    async def get_profile(
        session: UserSession, profiles: HermesProfileRepository
    ) -> str:
        """профиль hermes текущего пользователя; запросы идут на /p/<профиль>/

        имя закрепляется за пользователем при первом входе и дальше не
        меняется, поэтому его достаточно взять один раз за сессию
        """
        if profile := session.get(ChainlitAdapter.PROFILE_KEY):
            return str(profile)

        user = ChainlitAdapter.get_chat_user(session)
        profile = await profiles.ensure(
            ChainlitAdapter.get_user_id(session), user.identifier
        )
        session.set(ChainlitAdapter.PROFILE_KEY, profile)

        return profile


@cl.on_message
@chainlit_error_ctx_handler
@di_inject
async def on_message(
    msg: cl.Message,
    api: Annotated[HermesApi, Depends(hermes_api)],
    profiles: Annotated[
        HermesProfileRepository,
        Depends(hermes_profile_repository, scope="session"),
    ],
):
    # id одного диалога; он же id сессии hermes, в которой идёт ход
    thread_id = ChainlitAdapter.get_chat_id(cl.context.session)
    profile = await ChainlitAdapter.get_profile(cl.user_session, profiles)

    await HermesTurn(api.of(profile)).run(thread_id, msg.content)


@cl.on_chat_start
@chainlit_error_ctx_handler
@di_inject
async def on_chat_start(
    profiles: Annotated[
        HermesProfileRepository,
        Depends(hermes_profile_repository, scope="session"),
    ],
):
    user = ChainlitAdapter.get_chat_user(cl.user_session)
    await ChainlitAdapter.get_profile(cl.user_session, profiles)
    await cl.Message(f"Hello {user}").send()


@cl.on_logout
def on_logout(request: Request, response: Response):
    """вызывается, когда пользователь нажимает Logout"""
    for cookie_name in request.cookies:
        response.delete_cookie(cookie_name)


@cl.on_stop
async def on_stop():
    user = ChainlitAdapter.get_chat_user(cl.user_session)
    logger.info(f"{user.identifier} has stopped the task!")
    await cl.Message("You have stopped the task!").send()


@cl.on_chat_end
def on_chat_end():
    user = ChainlitAdapter.get_chat_user(cl.user_session)
    logger.info(f"{user.identifier} has ended the chat")


@cl.data_layer
@di_inject
def get_data_layer(
    data_layer: Annotated[BaseDataLayer, Depends(hermes_data_layer)],
) -> BaseDataLayer:
    return data_layer


@cl.on_chat_resume
@chainlit_error_ctx_handler
@di_inject
async def on_chat_resume(
    thread_dict: ThreadDict,
    profiles: Annotated[
        HermesProfileRepository,
        Depends(hermes_profile_repository, scope="session"),
    ],
):
    """Возобновление треда.

    Историю chainlit уже показал: её отдал data layer из hermes. Здесь
    нужен только профиль в сессии — под ним пойдёт следующий ход. Без
    самого callback'а chainlit открывает старый тред только на чтение.
    """
    await ChainlitAdapter.get_profile(cl.user_session, profiles)
