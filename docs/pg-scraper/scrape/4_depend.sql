-- @name depend
-- @wave 4
-- @params rels attrdefs procs types
-- @key classid, objid, objsubid, refclassid, refobjid, refobjsubid, deptype
select classid, objid, objsubid, refclassid, refobjid, refobjsubid, deptype, xmin::text as row_xmin
from pg_depend
where (refclassid = 1259 and refobjid = any($1))
   or (classid = 2604 and objid = any($2))
   or (deptype = 'e' and objid = any($1))
   or (deptype = 'e' and objid = any($3))
   or (deptype = 'e' and objid = any($4));
-- @verify
select classid, objid, objsubid, refclassid, refobjid, refobjsubid, deptype, xmin::text as row_xmin
from pg_depend
where (refclassid = 1259 and refobjid = any($1))
   or (classid = 2604 and objid = any($2))
   or (deptype = 'e' and objid = any($1))
   or (deptype = 'e' and objid = any($3))
   or (deptype = 'e' and objid = any($4));
