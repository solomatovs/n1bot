create table if not exists {schema}.connections (
    id   uuid primary key default gen_random_uuid(),
    name text not null,
    data jsonb not null default '{{}}'::jsonb
);

create index if not exists idx_connections_kind
    on {schema}.connections ((data ->> 'kind'));
