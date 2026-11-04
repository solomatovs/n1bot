create table if not exists {schema}.users (
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

-- регистр логина не заводит вторую личность: инвариант держит база
create unique index if not exists idx_users_identifier_lower
    on {schema}.users (lower(identifier));

create table if not exists {schema}.roles (
    id        uuid primary key default gen_random_uuid(),
    role      varchar not null unique,
    create_at timestamptz not null default now()
);

create table if not exists {schema}.user_roles (
    user_id uuid not null references {schema}.users(id) on delete cascade,
    role_id uuid not null references {schema}.roles(id) on delete cascade,
    managed boolean not null default false,
    primary key (user_id, role_id)
);

create index if not exists idx_user_roles_role on {schema}.user_roles (role_id);
