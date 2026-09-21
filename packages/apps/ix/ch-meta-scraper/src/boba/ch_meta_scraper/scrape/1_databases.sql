-- @name databases
-- @wave 1
-- @collect dbs name
select
    name,
    engine,
    engine_full,
    uuid,
    comment,
    hex(sipHash64(tuple(name, engine, engine_full, uuid, comment))) as row_version
from
    system.databases
where
    name not in ('system', 'INFORMATION_SCHEMA', 'information_schema')
-- @verify
select
    name,
    hex(sipHash64(tuple(name, engine, engine_full, uuid, comment))) as row_version
from
    system.databases
where
    name not in ('system', 'INFORMATION_SCHEMA', 'information_schema')
