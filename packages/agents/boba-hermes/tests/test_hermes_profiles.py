from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from boba.hermes.chat.data import HermesProfileRepository, PostgresDataLayer

pytestmark = pytest.mark.anyio


@pytest.fixture
async def repo(
    layer: PostgresDataLayer, pool: AsyncConnectionPool, test_schema: str
) -> HermesProfileRepository:
    # layer поднимает свежую схему со всеми таблицами, включая hermes_profiles
    return HermesProfileRepository(pool, schema=test_schema)


async def test_ensure_takes_readable_name(repo: HermesProfileRepository):
    user_id = uuid4()

    profile = await repo.ensure(user_id, "Solomatov.SI")

    # имя должно быть узнаваемым в hermes CLI, а не uuid
    assert profile == "solomatov-2esi"
    assert await repo.get(user_id) == profile


async def test_ensure_is_idempotent(repo: HermesProfileRepository):
    user_id = uuid4()

    first = await repo.ensure(user_id, "solomatovs")
    second = await repo.ensure(user_id, "solomatovs")

    assert first == second


async def test_ensure_keeps_profile_after_login_change(repo: HermesProfileRepository):
    user_id = uuid4()
    profile = await repo.ensure(user_id, "old.login")

    # переименование в AD не должно уводить пользователя в чужую историю
    assert await repo.ensure(user_id, "new.login") == profile


async def test_ensure_rejects_name_taken_by_another_user(
    repo: HermesProfileRepository,
):
    first_user, second_user = uuid4(), uuid4()
    await repo.ensure(first_user, "duplicate.login")

    # одинаковых логинов быть не может (users.identifier уникален), так что это
    # разъехавшееся состояние, а не рядовая ситуация: молча увести второго
    # пользователя в чужую историю нельзя
    with pytest.raises(RuntimeError, match="taken by another user"):
        await repo.ensure(second_user, "duplicate.login")


# async def test_ensure_accepts_non_latin_login(repo: HermesProfileRepository):
#     user_id = uuid4()

#     # кириллица уходит в escape, имя остаётся валидным для hermes
#     profile = await repo.ensure(user_id, "Иванов")

#     assert profile == "u-d0-b8-d0-b2-d0-b0-d0-bd-d0-be-d0-b2"
#     assert await repo.user_of(profile) == user_id


# async def test_user_of_resolves_owner(repo: HermesProfileRepository):
#     user_id = uuid4()
#     profile = await repo.ensure(user_id, "owner.lookup")

#     assert await repo.user_of(profile) == user_id


# async def test_lookups_miss_on_unknown(repo: HermesProfileRepository):
#     assert await repo.get(uuid4()) is None
#     assert await repo.user_of("нет-такого-профиля") is None


# async def test_user_of_returns_uuid(repo: HermesProfileRepository):
#     user_id = uuid4()
#     profile = await repo.ensure(user_id, "Петров")

#     owner = await repo.user_of(profile)

#     assert isinstance(owner, UUID)
#     assert owner == user_id
