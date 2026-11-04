-- body — json, а не jsonb: jsonb переупорядочивает ключи, а порядок
-- аргументов и колонок результата виден пользователю
create unlogged table if not exists {schema}.live_payloads (
    scope_kind text not null,
    scope_id   uuid not null,
    id         uuid primary key,
    body       json not null,
    at         timestamptz not null default now()
);

alter table {schema}.live_payloads
    alter column body type json using body::text::json,
    drop constraint if exists live_payloads_scope_kind_check,
    add constraint live_payloads_scope_kind_check
        check (scope_kind in ('chat', 'workflow', 'job', 'user'));

create index if not exists idx_live_payloads_scope
    on {schema}.live_payloads (scope_kind, scope_id);
