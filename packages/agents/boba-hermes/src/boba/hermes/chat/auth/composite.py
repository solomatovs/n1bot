from collections.abc import Awaitable, Callable

import chainlit as cl
from chainlit.config import config as chainlit_config

from boba.hermes.errors import AuthenticationError

UserCallback = Callable[..., Awaitable[cl.User | None]]


class PasswordAuthCallbackInstaller:
    def __init__(self):
        self._auth = []

    def local_auth_setup(self, local_auth) -> None:
        self._auth.append(local_auth)

    def ldap_auth_setup(self, ldap_auth) -> None:
        self._auth.append(ldap_auth)

    def install_callback_if_any_exists(self) -> None:
        # устанавливаю callback только если есть авторизатор
        if self._auth:
            # добавляю callback через chainlit_config.code,
            # а не через cl.password_auth_callback
            # потому что иначе chaionlit проглатывает любые exception'ы
            # и пишет что введен некорректный пароль
            # вместо того, что бы писать реальную проблему
            chainlit_config.code.password_auth_callback = self._build_callback()

    def _build_callback(self) -> UserCallback:
        async def password_auth(username: str, password: str) -> cl.User | None:
            last_error: AuthenticationError | None = None

            for auth in self._auth:
                try:
                    res = await auth.password_auth(username, password)
                    if res is not None:
                        return res

                # единственный тип ошибки, который отлавливается, накапливается
                # и если ни один сервис не авторизовал, то выбрасывает
                # накопленную ошибку
                except AuthenticationError as e:
                    last_error = e

            # если была ошибка в процессе авторизации
            # и после всех проверок не удалось авторизовать пользователя
            # возвращаю последнюю ошибку
            if last_error:
                raise last_error

            return None

        return password_auth
