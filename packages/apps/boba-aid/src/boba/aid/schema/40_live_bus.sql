create unlogged table if not exists {schema}.live_instances (
    instance_id  text primary key,
    app          text not null check (app in ('aid')),
    host         text not null,
    started_at   timestamptz not null default now(),
    heartbeat_at timestamptz not null default now()
);

create unlogged table if not exists {schema}.live_events (
    scope_kind text not null,
    scope_id   uuid not null,
    seq        bigint not null,
    kind       text not null,
    origin     text not null,
    body       jsonb not null,
    at         timestamptz not null default now(),
    primary key (scope_kind, scope_id, seq)
);

alter table {schema}.live_events
    drop constraint if exists live_events_scope_kind_check,
    add constraint live_events_scope_kind_check
        check (scope_kind in ('chat', 'workflow', 'job', 'user'));

-- номер события — из общей последовательности: после уборки строк области
-- seq не начинается заново; последовательность уже созданной таблицы один раз
-- догоняет max(seq), пока не тронута
create sequence if not exists {schema}.live_events_seq as bigint;

select setval('{schema}.live_events_seq', m.next, false)
from (
    select coalesce(max(seq), 0) + 1 as next
    from {schema}.live_events
) m
where not (select is_called from {schema}.live_events_seq);

create unlogged table if not exists {schema}.live_commands (
    id          bigint generated always as identity primary key,
    scope_kind  text not null,
    scope_id    uuid not null,
    action      text not null check (action in ('stop')),
    body        jsonb not null,
    by_instance text not null,
    at          timestamptz not null default now(),
    taken_by    text,
    taken_at    timestamptz
);

alter table {schema}.live_commands
    drop constraint if exists live_commands_by_instance_fkey,
    drop constraint if exists live_commands_taken_by_fkey,
    drop constraint if exists live_commands_scope_kind_check,
    add constraint live_commands_scope_kind_check
        check (scope_kind in ('chat', 'workflow', 'job', 'user'));

create index if not exists idx_live_commands_scope
    on {schema}.live_commands (scope_kind, scope_id);
