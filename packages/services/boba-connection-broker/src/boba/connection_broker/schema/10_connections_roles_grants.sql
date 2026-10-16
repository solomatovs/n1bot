create table if not exists {schema}.connections (
    id   uuid primary key default gen_random_uuid(),
    name text not null,
    data jsonb not null default '{{}}'::jsonb
);

create index if not exists idx_connections_kind
    on {schema}.connections ((data ->> 'kind'));

create table if not exists {schema}.roles (
    id        uuid primary key default gen_random_uuid(),
    role      varchar not null unique,
    create_at timestamptz not null default now()
);

create table if not exists {schema}.grants (
    id          uuid primary key default gen_random_uuid(),
    src_kind    varchar not null,
    src_kind_id uuid not null,
    tgt_kind    varchar not null,
    tgt_kind_id uuid not null,
    unique (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
);

create index if not exists idx_grants_target
    on {schema}.grants (tgt_kind, tgt_kind_id);
