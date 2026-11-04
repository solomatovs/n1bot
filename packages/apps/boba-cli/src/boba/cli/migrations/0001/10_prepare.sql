do $migration$
begin
    if exists (select 1 from information_schema.columns
               where table_schema = {schema_name_lit} and table_name = 'users' and column_name = 'meta') then
        alter table {schema}.users add column if not exists realm text, add column if not exists login text;
        update {schema}.users
        set realm = meta -> 'identity' ->> 'realm', login = meta -> 'identity' ->> 'login'
        where meta ? 'identity' and realm is null and login is null;
    end if;
    if to_regclass(format('%I.user_roles', {schema_name_lit})) is not null and not exists (
        select 1 from information_schema.columns where table_schema = {schema_name_lit}
        and table_name = 'user_roles' and column_name = 'managed'
    ) then
        alter table {schema}.user_roles add column managed boolean not null default false;
        update {schema}.user_roles set managed = true;
    end if;
end
$migration$;

do $migration$
begin
    if exists (select 1 from information_schema.columns where table_schema = {schema_name_lit}
               and table_name = 'session' and column_name = 'owner') then
        create table if not exists {schema}.users (id uuid primary key default gen_random_uuid(), identifier text not null unique);
        alter table {schema}.session add column if not exists user_id uuid;
        insert into {schema}.users (identifier)
            select distinct owner from {schema}.session where owner is not null and owner <> ''
            on conflict (identifier) do nothing;
        update {schema}.session s set user_id = u.id from {schema}.users u
            where s.owner = u.identifier and s.user_id is null;
    end if;
end
$migration$;
