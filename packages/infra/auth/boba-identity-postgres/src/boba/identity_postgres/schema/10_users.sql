create table if not exists {schema}.users (
    id         uuid primary key default gen_random_uuid(),
    identifier text not null unique,
    created_at timestamptz not null default now(),
    meta       jsonb not null default '{{}}'::jsonb
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
    primary key (user_id, role_id)
);

create index if not exists idx_user_roles_role on {schema}.user_roles (role_id);
