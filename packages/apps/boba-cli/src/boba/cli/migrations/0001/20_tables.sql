create temporary table pg_temp.users (
    id         uuid primary key default gen_random_uuid(),
    identifier text not null unique,
    created_at timestamptz not null default now(),
    realm      text,
    login      text,
    constraint users_identity check (
        (realm is null and login is null)
        or (realm is not null and login is not null
            and realm ~ '^[a-z0-9][a-z0-9._-]*$'
            and length(login) > 0 and login = lower(btrim(login))
            and identifier = realm || ':' || login)
    )
);

create temporary table pg_temp.roles (
    id        uuid primary key default gen_random_uuid(),
    role      varchar not null unique,
    create_at timestamptz not null default now()
);

create temporary table pg_temp.user_roles (
    user_id uuid not null references pg_temp.users(id) on delete cascade,
    role_id uuid not null references pg_temp.roles(id) on delete cascade,
    managed boolean not null default false,
    primary key (user_id, role_id)
);

create unique index if not exists idx_users_identifier_lower
    on pg_temp.users (lower(identifier));

create index if not exists idx_user_roles_role on pg_temp.user_roles (role_id);

create temporary table pg_temp.session (
    id         text        primary key,
    created_at timestamptz not null,
    user_id    uuid references pg_temp.users(id)
);

create temporary table pg_temp.session_records (
    session_id  text        not null references pg_temp.session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);

create index if not exists session_user_id_id_idx
    on pg_temp.session (user_id, id);

comment on table pg_temp.session is
'Сессии агента. user_id ссылается на users; NULL означает историю локального stdio без пользователя службы.';

comment on column pg_temp.session.user_id is
'UUID пользователя службы. Удаление пользователя с историей запрещает внешний ключ.';

comment on table pg_temp.session_records is
'Записи истории сессии. Пользователь определяется через session_id, удаление сессии удаляет её записи.';
