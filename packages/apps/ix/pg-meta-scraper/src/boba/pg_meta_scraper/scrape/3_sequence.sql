-- @name sequence
-- @wave 3
-- @params rels
-- @key seqrelid
-- @min 100000
select
    seqrelid,
    seqtypid,
    format_type(seqtypid, null) as data_type,
    seqstart,
    seqincrement,
    seqmin,
    seqmax,
    seqcache,
    seqcycle,
    xmin::text as row_xmin
from
    pg_sequence
where
    seqrelid = any(%(rels)s::oid[]);
-- @verify
select
    seqrelid,
    xmin::text as row_xmin
from
    pg_sequence
where
    seqrelid = any(%(rels)s::oid[]);
