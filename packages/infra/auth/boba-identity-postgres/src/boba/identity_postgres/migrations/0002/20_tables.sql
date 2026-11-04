create temporary table pg_temp.users (
    id         uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    realm      text not null,
    login      text not null,
    constraint users_realm_login_key unique (realm, login),
    constraint users_identity check (
        realm ~ '^[a-z0-9][a-z0-9._-]*$'
        and length(login) > 0 and login = lower(btrim(login))
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

create index idx_user_roles_role on pg_temp.user_roles (role_id);
