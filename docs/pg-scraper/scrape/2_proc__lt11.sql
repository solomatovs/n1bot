-- @name proc
-- @wave 2
-- @params schemas
-- @key oid
-- @collect procs oid
-- @max 109999
select oid, proname, pronamespace, proowner, pg_get_userbyid(proowner) as owner_name, prolang,
       case when proisagg then 'a' when proiswindow then 'w' else 'f' end as prokind, prorettype, proretset, provolatile, prosecdef,
       array(select unnest(proargtypes::oid[])) as proargtypes, proargnames, proargmodes,
       pg_get_function_identity_arguments(oid) as identity_args, pg_get_function_result(oid) as result_type, xmin::text as row_xmin
from pg_proc
where pronamespace = any($1);
-- @verify
select oid, xmin::text as row_xmin
from pg_proc
where pronamespace = any($1);
