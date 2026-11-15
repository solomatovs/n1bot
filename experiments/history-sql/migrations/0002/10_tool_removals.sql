create index if not exists records_tool_removals
    on {schema}.records (session_id, key, seq)
    where 1=1
        and category = 'tool_catalog'
        and operation = 'remove';
