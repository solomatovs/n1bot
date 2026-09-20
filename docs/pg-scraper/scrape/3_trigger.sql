-- @name trigger
-- @wave 3
-- @params rels
-- @key oid
-- @collect triggers oid
-- @min 130000
select oid, tgrelid, tgname, tgfoid, tgtype, tgenabled, tgconstraint, tgconstrrelid, tgparentid as tgparentid,
       array(select unnest(tgattr::int2[])) as tgattr, xmin::text as row_xmin
from pg_trigger
where tgrelid = any($1) and not tgisinternal;
-- @verify
select oid, xmin::text as row_xmin
from pg_trigger
where tgrelid = any($1) and not tgisinternal;
