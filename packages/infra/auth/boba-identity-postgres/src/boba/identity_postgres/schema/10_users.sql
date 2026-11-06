create table if not exists {schema}.users (
    id         uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    login      text not null,
    constraint users_login_key unique (login),
    constraint users_identity check (
        length(login) > 0 and login = lower(btrim(login))
    )
);

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
