import re

import pytest

from boba.hermes.chat.data.profiles import HermesProfileEncoder

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Кодеку не нужны ни chainlit-контекст, ни postgres.

    Подменяет autouse-фикстуру из conftest, которая поднимает их для тестов
    data layer.
    """


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("solomatovs", "solomatovs"),
        ("Solomatov_SI", "solomatov_si"),
        ("LOSHARA\\Solomatov.SI", "loshara-5csolomatov-2esi"),
        ("solomatovs@LOSHARA.COM", "solomatovs-40loshara-2ecom"),
        ("  solomatovs  ", "solomatovs"),
        # hermes требует первым символом букву или цифру, escape начинается с дефиса
        ("@LOSHARA.COM", "u-40loshara-2ecom"),
        (".hidden", "u-2ehidden"),
        ("Иванов", "u-d0-b8-d0-b2-d0-b0-d0-bd-d0-be-d0-b2"),
    ],
)
def test_encode(username: str, expected: str):
    assert HermesProfileEncoder().encode(username) == expected


@pytest.mark.parametrize(
    "username",
    [
        "u" * 200,
        "Иванов-Петров-Сидоров",
        "@" * 100,
    ],
)
def test_encode_keeps_hermes_length_limit(username: str):
    # длинный логин не ошибка: имя просто обрезается до предела hermes
    assert len(HermesProfileEncoder().encode(username)) == 64


@pytest.mark.parametrize(
    "username", ["solomatovs", "Иванов", "@LOSHARA.COM", "u" * 200]
)
def test_encode_always_matches_hermes_format(username: str):
    # ровно тот формат, который проверяет hermes_cli.profiles._PROFILE_ID_RE
    name = HermesProfileEncoder().encode(username)

    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name)
