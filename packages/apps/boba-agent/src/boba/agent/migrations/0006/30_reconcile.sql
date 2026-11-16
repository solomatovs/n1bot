select pg_temp.reconcile_tables({schema_name_lit}, array[
    'pg_temp.users'::regclass,
    'pg_temp.roles'::regclass,
    'pg_temp.user_roles'::regclass,
    'pg_temp.sessions'::regclass,
    'pg_temp.records'::regclass,
    'pg_temp.providers'::regclass
], array[]::text[]);
