-- @name class
-- @wave 2
-- @params schemas
-- @collect rels oid
-- @max 90099
-- @not gp
select
    oid,
    relname,
    relnamespace,
    relkind,
    relowner,
    pg_get_userbyid(relowner) as owner_name,
    relam,
    reltype,
    reloftype,
    relnatts,
    relchecks,
    relhasindex,
    relhastriggers,
    relhassubclass,
    false as relispartition,
    null::text as relstorage,
    reltablespace,
    'p' as relpersistence,
    null::text as relpartbound,
    reltuples::float8 as reltuples,
    relpages,
    xmin::text as row_xmin
from
    pg_class
where
    relnamespace = any(%(schemas)s::oid[])
    and relkind in ('r', 'p', 'v', 'm', 'f', 'S', 'i', 'I', 'c');
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_class
where
    relnamespace = any(%(schemas)s::oid[])
    and relkind in ('r', 'p', 'v', 'm', 'f', 'S', 'i', 'I', 'c');
