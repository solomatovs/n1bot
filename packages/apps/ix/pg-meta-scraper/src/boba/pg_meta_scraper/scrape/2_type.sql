select
    oid,
    typname,
    typnamespace,
    typowner,
    typtype,
    typcategory,
    typrelid,
    typbasetype,
    typelem,
    typnotnull,
    case when typbasetype <> 0 then format_type(typbasetype, typtypmod) end as base_type,
    xmin::text as row_xmin
from
    pg_type
where
    typnamespace = any(%(schemas)s::oid[])
