create table if not exists {schema}.threads (
    id         uuid primary key,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    name       text,
    user_id    uuid,
    tags       text[],
    meta       jsonb
);

alter table {schema}.threads add column if not exists updated_at timestamptz;

update {schema}.threads set updated_at = created_at where updated_at is null;

alter table {schema}.threads alter column updated_at set not null;

create index if not exists idx_threads_user_id
    on {schema}.threads (user_id);

create index if not exists idx_threads_user_updated
    on {schema}.threads (user_id, updated_at desc, id desc);
