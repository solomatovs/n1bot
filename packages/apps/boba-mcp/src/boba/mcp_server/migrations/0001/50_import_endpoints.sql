do $migration$
declare
    entry jsonb;
    endpoint_id uuid;
    role_name text;
    input jsonb := coalesce(coalesce(nullif(current_setting('boba.migration.data', true), ''), '{{}}')::jsonb->'endpoints', '[]'::jsonb);
begin
    for entry in select value from jsonb_array_elements(input) loop
        if jsonb_typeof(entry->'tools') is distinct from 'array'
           or jsonb_typeof(entry->'roles') is distinct from 'array' then
            raise exception 'Endpoint %: tools and roles must be JSON arrays', entry->>'prefix_url';
        end if;
        if exists (select 1 from jsonb_array_elements(entry->'tools') t(name)
                   where jsonb_typeof(t.name) <> 'string' or t.name = '"*"'::jsonb) then
            raise exception 'Endpoint %: tools must contain exact exported names, without wildcards', entry->>'prefix_url';
        end if;
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

end
$migration$;
