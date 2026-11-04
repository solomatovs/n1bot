create table if not exists {schema}.mcp_endpoints (
    id uuid primary key default gen_random_uuid(),
    prefix_url text not null unique,
    title text not null,
    description text not null default '',
    instructions text not null default '',
    enabled boolean not null default true,
    tools jsonb not null default '[]',
    overrides jsonb not null default '{{}}',
    prompts jsonb not null default '{{}}',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

do $migration$
begin
    if exists (
        select 1 from pg_attribute
        where attrelid = '{schema}.mcp_endpoints'::regclass
          and attname = 'name' and not attisdropped
    ) then
        alter table {schema}.mcp_endpoints rename column name to prefix_url;
        update {schema}.mcp_endpoints set prefix_url = '/' || ltrim(prefix_url, '/');
    end if;
end
$migration$;

alter table {schema}.mcp_endpoints
    add column if not exists title text,
    add column if not exists instructions text not null default '',
    add column if not exists description text not null default '',
    add column if not exists enabled boolean not null default true,
    add column if not exists tools jsonb not null default '[]',
    add column if not exists overrides jsonb not null default '{{}}',
    add column if not exists prompts jsonb not null default '{{}}',
    add column if not exists created_at timestamptz not null default now(),
    add column if not exists updated_at timestamptz not null default now();

update {schema}.mcp_endpoints set title = prefix_url where title is null;
alter table {schema}.mcp_endpoints alter column title set not null;

do $migration$
begin
    if to_regclass('{schema}.mcp_endpoint_tools') is not null then
        update {schema}.mcp_endpoints e
        set tools = (
            select coalesce(jsonb_agg(t.tool order by t.tool), '[]'::jsonb)
            from {schema}.mcp_endpoint_tools t where t.endpoint_id = e.id
        );
    end if;
end
$migration$;

alter table {schema}.mcp_endpoints
    add constraint mcp_endpoints_path check (
        prefix_url ~ '^(/[A-Za-z0-9_~.-]+)+$'
        and prefix_url !~ '(^|/)\.\.?(/|$)'
    ),
    add constraint mcp_endpoints_tools_array check (jsonb_typeof(tools) = 'array'),
    add constraint mcp_endpoints_overrides_object check (jsonb_typeof(overrides) = 'object'),
    add constraint mcp_endpoints_prompts_object check (jsonb_typeof(prompts) = 'object');

create unique index if not exists idx_mcp_endpoints_prefix_url
    on {schema}.mcp_endpoints (prefix_url);

create function {schema}.mcp_endpoint_updated() returns trigger language plpgsql as $body$
begin
    new.updated_at = now();
    return new;
end
$body$;

create trigger mcp_endpoint_updated before update on {schema}.mcp_endpoints
    for each row execute function {schema}.mcp_endpoint_updated();
