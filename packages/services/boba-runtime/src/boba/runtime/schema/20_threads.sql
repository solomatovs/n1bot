create table if not exists {schema}.threads (
    id         uuid primary key,
    created_at timestamptz not null,
    name       text,
    user_id    uuid,
    tags       text[],
    meta       jsonb
);

create index if not exists idx_threads_user_id
    on {schema}.threads (user_id);
