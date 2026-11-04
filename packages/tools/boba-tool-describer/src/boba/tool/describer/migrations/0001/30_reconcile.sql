select pg_temp.reconcile_tables({schema_name_lit}, array[
    'pg_temp.node'::regclass,
    'pg_temp.edge'::regclass
], array[]::text[]);
