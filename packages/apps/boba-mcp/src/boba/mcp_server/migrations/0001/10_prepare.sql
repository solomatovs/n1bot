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
    if to_regclass(format('%I.mcp_endpoints', {schema_name_lit})) is not null then
        if exists (select 1 from information_schema.columns where table_schema = {schema_name_lit}
                   and table_name = 'mcp_endpoints' and column_name = 'name')
           and not exists (select 1 from information_schema.columns where table_schema = {schema_name_lit}
                   and table_name = 'mcp_endpoints' and column_name = 'prefix_url') then
            alter table {schema}.mcp_endpoints rename column name to prefix_url;
            update {schema}.mcp_endpoints set prefix_url = '/' || ltrim(prefix_url, '/');
        end if;
        alter table {schema}.mcp_endpoints add column if not exists title text,
            add column if not exists tools jsonb not null default '[]';
        update {schema}.mcp_endpoints set title = prefix_url where title is null;
    end if;
end
$migration$;
