do $migration$
declare
    entry jsonb;
    endpoint_id uuid;
    role_name text;
    conflicting record;
    input jsonb := coalesce(nullif(current_setting('boba.mcp.endpoint_import', true), ''), '[]')::jsonb;
begin
    for entry in select value from jsonb_array_elements(input) loop
        for role_name in select jsonb_array_elements_text(entry->'roles') loop
            if role_name <> '*' and not exists (
                select 1 from {schema}.roles where role = role_name
            ) then
                raise exception 'Endpoint %: role % does not exist in the MCP role catalog', entry->>'prefix_url', role_name;
            end if;
        end loop;

        insert into {schema}.mcp_endpoints
            (prefix_url, title, description, instructions, tools, prompts)
        values
            (entry->>'prefix_url', entry->>'title', entry->>'description',
             entry->>'instructions', entry->'tools', entry->'prompts')
        on conflict (prefix_url) do nothing;

        select id into strict endpoint_id from {schema}.mcp_endpoints
        where prefix_url = entry->>'prefix_url';

        if exists (
            select 1 from {schema}.mcp_endpoints
            where id = endpoint_id and
                (tools <> entry->'tools' or prompts <> entry->'prompts')
        ) then
            raise exception 'Endpoint % already exists with different tools or prompts; resolve the import before retrying', entry->>'prefix_url';
        end if;

        insert into {schema}.grants (src_kind, src_kind_id, tgt_kind, tgt_kind_id)
        select 'mcp_endpoints', endpoint_id, 'roles', r.id
        from {schema}.roles r
        where (entry->'roles') ? '*' or (entry->'roles') ? r.role
        on conflict (src_kind, src_kind_id, tgt_kind, tgt_kind_id) do nothing;
    end loop;

    if to_regclass('{schema}.role_tools') is null then
        return;
    end if;

    if exists (select 1 from {schema}.role_tools)
       and not exists (select 1 from {schema}.mcp_endpoints) then
        raise exception 'MCP still has role_tools but no endpoints. Run migrate with --import-endpoints before removing the legacy policy';
    end if;

    for conflicting in
        select e.prefix_url, r.role, t.name
        from {schema}.mcp_endpoints e
        join {schema}.grants g on g.src_kind = 'mcp_endpoints'
            and g.src_kind_id = e.id and g.tgt_kind = 'roles'
        join {schema}.roles r on r.id = g.tgt_kind_id
        cross join lateral jsonb_array_elements_text(e.tools) t(name)
        where t.name not in ('file_upload', 'workflow')
          and not exists (
            select 1 from {schema}.role_tools rt
            where rt.role_id = r.id and rt.tool in ('*', t.name)
          )
    loop
        raise exception 'Endpoint % would grant tool % to role %, which legacy role_tools did not allow. Split endpoint tool sets before migration',
            conflicting.prefix_url, conflicting.name, conflicting.role;
    end loop;
end
$migration$;
