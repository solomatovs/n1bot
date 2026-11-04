select pg_temp.reconcile_tables({schema_name_lit}, array[
    'pg_temp.users'::regclass,
    'pg_temp.roles'::regclass,
    'pg_temp.user_roles'::regclass
]);
