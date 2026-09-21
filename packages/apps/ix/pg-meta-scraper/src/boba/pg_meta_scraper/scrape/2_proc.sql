-- @name proc
-- @wave 2
-- @params schemas
-- @collect procs oid
-- @min 110000
select
    oid,
    proname,
    pronamespace,
    proowner,
    pg_get_userbyid(proowner) as owner_name,
    prolang,
    prokind as prokind,
    prorettype,
    proretset,
    provolatile,
    prosecdef,
    array(select unnest(proargtypes::oid[])) as proargtypes,
    proargnames,
    proargmodes,
    pg_get_function_identity_arguments(oid) as identity_args,
    pg_get_function_result(oid) as result_type,
    xmin::text as row_xmin
from
    pg_proc
where
    pronamespace = any(%(schemas)s::oid[]);
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_proc
where
    pronamespace = any(%(schemas)s::oid[]);
