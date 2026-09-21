-- @name server
-- @wave 1
select
    version() as version,
    hex(sipHash64(version())) as row_version
-- @verify
select
    version() as version,
    hex(sipHash64(version())) as row_version
