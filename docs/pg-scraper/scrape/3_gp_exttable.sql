-- @name gp_exttable
-- @wave 3
-- @params rels
-- @key reloid
-- @only gp
-- @max 99999
select reloid, urilocation, fmttype, xmin::text as row_xmin
from pg_exttable
where reloid = any($1);
-- @verify
select reloid, xmin::text as row_xmin
from pg_exttable
where reloid = any($1);
