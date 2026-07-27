from pathlib import Path

import pytest

from boba.hermes.agent.profile import HermesProfileName, HermesProfileProvisioner
from boba.hermes.errors import InternalServiceError
from boba.hermes.infra.config import HermesConfig

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Провижионер работает с файлами: chainlit-контекст и postgres ему не нужны.

    Подменяет autouse-фикстуру из conftest, которая поднимает их для тестов
    data layer.
    """


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    """HERMES_HOME донора: то, что видит контейнер агента."""
    (tmp_path / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test\n")
    return tmp_path


@pytest.fixture
def provisioner(hermes_home: Path) -> HermesProfileProvisioner:
    return HermesProfileProvisioner(
        HermesConfig(
            base_url="http://hermes:8642",
            api_key="k" * 32,
            data_dir=str(hermes_home),
        )
    )


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("solomatovs", "solomatovs"),
        ("Solomatov_SI", "solomatov_si"),
        ("LOSHARA\\Solomatov.SI", "loshara-5csolomatov-2esi"),
        ("solomatovs@LOSHARA.COM", "solomatovs-40loshara-2ecom"),
        ("  solomatovs  ", "solomatovs"),
    ],
)
def test_encode(username: str, expected: str):
    assert HermesProfileName().encode(username) == expected


@pytest.mark.parametrize(
    "username",
    [
        "solomatovs",
        "LOSHARA\\Solomatov.SI",
        "solomatovs@LOSHARA.COM",
        "user-with-dash",
        "user.Иванов",
    ],
)
def test_encode_decode_roundtrip(username: str):
    name = HermesProfileName()
    # без профиля на диске остаётся только кодек, а он не хранит регистр
    assert name.decode(name.encode(username)) == username.strip().lower()


@pytest.mark.parametrize(
    "username",
    [
        "@LOSHARA.COM",
        ".hidden",
        "",
        "u" * 65,
        # hermes требует [a-z0-9] первым символом, а escape начинается с дефиса
        "Иванов",
    ],
)
def test_encode_rejects_unusable(username: str):
    # молча подставить запасное имя нельзя: попадём в чужой профиль
    with pytest.raises(InternalServiceError):
        HermesProfileName().encode(username)


@pytest.mark.parametrize("profile", ["solomatovs-2", "solomatovs-zz", "-c3-28"])
def test_decode_rejects_broken_escape(profile: str):
    with pytest.raises(InternalServiceError):
        HermesProfileName().decode(profile)


async def test_username_restores_original_login(
    provisioner: HermesProfileProvisioner,
):
    profile = await provisioner.ensure("Solomatov.SI")

    # регистр приходит из .username, кодек вернул бы solomatov-2esi
    assert await provisioner.username(profile) == "Solomatov.SI"


async def test_username_falls_back_to_codec(
    provisioner: HermesProfileProvisioner, hermes_home: Path
):
    profile = await provisioner.ensure("solomatovs")
    (hermes_home / "profiles" / profile / ".username").unlink()

    # профиль, заведённый мимо провижионера, всё равно читается
    assert await provisioner.username(profile) == "solomatovs"


def test_encode_keeps_hermes_length_limit():
    # кириллица стоит по три символа на букву, в лимит hermes она не влезает
    with pytest.raises(InternalServiceError):
        HermesProfileName().encode("Иванов-Петров-Сидоров")


async def test_ensure_creates_profile_with_seed_files(
    provisioner: HermesProfileProvisioner, hermes_home: Path
):
    name = await provisioner.ensure("LOSHARA\\Solomatov.SI")

    profile = hermes_home / "profiles" / name
    assert name == "loshara-5csolomatov-2esi"
    assert (profile / ".username").read_text() == "LOSHARA\\Solomatov.SI"
    assert (profile / "config.yaml").read_text().startswith("gateway:")
    # без .env у профиля нет ключей провайдера и агент падает на первом запросе
    assert (profile / ".env").read_text() == "OPENAI_API_KEY=sk-test\n"


async def test_ensure_is_idempotent(
    provisioner: HermesProfileProvisioner, hermes_home: Path
):
    name = await provisioner.ensure("solomatovs")
    (hermes_home / "profiles" / name / "state.db").write_text("история")

    assert await provisioner.ensure("solomatovs") == name
    # повторный вход не должен затирать накопленное состояние профиля
    assert (hermes_home / "profiles" / name / "state.db").read_text() == "история"


async def test_ensure_leaves_no_staging_dir(
    provisioner: HermesProfileProvisioner, hermes_home: Path
):
    await provisioner.ensure("solomatovs")

    # gateway перебирает profiles/ на каждом запросе: незавершённый каталог
    # не должен там остаться
    assert [p.name for p in (hermes_home / "profiles").iterdir()] == ["solomatovs"]


async def test_ensure_fails_when_home_not_mounted(tmp_path: Path):
    provisioner = HermesProfileProvisioner(
        HermesConfig(
            base_url="http://hermes:8642",
            api_key="k" * 32,
            data_dir=str(tmp_path / "нет-такого"),
        )
    )

    with pytest.raises(InternalServiceError):
        await provisioner.ensure("solomatovs")


async def test_ensure_fails_without_seed_file(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("gateway: {}\n")
    provisioner = HermesProfileProvisioner(
        HermesConfig(
            base_url="http://hermes:8642",
            api_key="k" * 32,
            data_dir=str(tmp_path),
        )
    )

    # .env у донора нет — профиль не создаётся вовсе, вместо профиля без ключей
    with pytest.raises(InternalServiceError):
        await provisioner.ensure("solomatovs")
    assert not (tmp_path / "profiles" / "solomatovs").exists()
