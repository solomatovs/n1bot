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
