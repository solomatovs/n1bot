-- @name foreign_server
-- @wave 1
-- @key oid
-- @min 90100
select oid, srvname, srvfdw, srvoptions, xmin::text as row_xmin
from pg_foreign_server;
-- @verify
select oid, xmin::text as row_xmin
from pg_foreign_server;
