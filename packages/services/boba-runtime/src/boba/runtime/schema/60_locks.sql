create unlogged table if not exists {schema}.live_locks (
    scope_kind   text not null,
    scope_id     uuid not null,
    mode         text not null check (mode in ('exclusive', 'shared')),
    holder       text not null
        references {schema}.live_instances (instance_id) on delete cascade,
    token        uuid not null,
    purpose      text not null check (purpose in ('turn', 'run', 'tool_call', 'cleanup')),
    user_id      uuid not null,
    acquired_at  timestamptz not null default now(),
    heartbeat_at timestamptz not null default now(),
    ttl_sec      integer not null check (ttl_sec > 0),
    primary key (scope_kind, scope_id, token)
);

alter table {schema}.live_locks
    drop constraint if exists live_locks_scope_kind_check,
    add constraint live_locks_scope_kind_check
        check (scope_kind in ('chat', 'workflow', 'job', 'user'));

create index if not exists idx_live_locks_scope
    on {schema}.live_locks (scope_kind, scope_id);
