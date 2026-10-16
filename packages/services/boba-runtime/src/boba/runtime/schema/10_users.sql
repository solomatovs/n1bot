create table if not exists {schema}.users (
    id         uuid primary key default gen_random_uuid(),
    identifier text not null unique,
    created_at timestamptz not null default now(),
    meta       jsonb not null default '{{}}'::jsonb
);

-- регистр логина не заводит вторую личность: инвариант держит база
create unique index if not exists idx_users_identifier_lower
    on {schema}.users (lower(identifier));
