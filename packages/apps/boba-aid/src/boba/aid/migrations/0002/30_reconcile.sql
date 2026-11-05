do $migration$
declare
    threads regclass;
    has_created_at boolean;
begin
    threads := to_regclass(format('%I.%I', {schema_name_lit}, 'threads'));
    if threads is null then
        return;
    end if;

    if not exists (
        select 1 from pg_attribute
        where attrelid = threads and attname = 'updated_at' and attnum > 0
          and not attisdropped
    ) then
        execute format('alter table %s add column updated_at timestamptz', threads);
    end if;

    select exists (
        select 1 from pg_attribute
        where attrelid = threads and attname = 'created_at' and attnum > 0
          and not attisdropped
    ) into has_created_at;

    if has_created_at then
        execute format(
            'update %s set updated_at = coalesce(updated_at, created_at, now()) '
            'where updated_at is null',
            threads
        );
    else
        execute format('update %s set updated_at = now() where updated_at is null', threads);
    end if;

    execute format('alter table %s alter column updated_at set not null', threads);
end
$migration$;

select pg_temp.reconcile_tables({schema_name_lit}, array[
    'pg_temp.users'::regclass,
    'pg_temp.roles'::regclass,
    'pg_temp.user_roles'::regclass,
    'pg_temp.grants'::regclass,
    'pg_temp.threads'::regclass,
    'pg_temp.feedbacks'::regclass,
    'pg_temp.live_instances'::regclass,
    'pg_temp.live_events'::regclass,
    'pg_temp.live_commands'::regclass,
    'pg_temp.live_payloads'::regclass,
    'pg_temp.live_locks'::regclass,
    'pg_temp.mcp_servers'::regclass,
    'pg_temp.mcp_tokens'::regclass
], array['live_instances', 'live_events', 'live_commands', 'live_payloads', 'live_locks']::text[]);
