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

comment on table {schema}.grants is
    'Shared grants between users and roles and polymorphic resources managed by an application.';
