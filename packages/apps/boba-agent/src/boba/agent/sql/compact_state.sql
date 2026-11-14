-- Persistent state copied after a successful summary. Include referenced catalog revisions.
with
params as (
    select
        $1::text as session_id,
        $2::bigint as through_seq
),
boundary as (
    select
        coalesce(max(r.seq), 0) as seq
    from
        params p
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.category = 'compact'
        and r.seq <= p.through_seq
),
latest as materialized (
    select distinct on (r.category, r.key)
        r.record_id,
        r.category,
        r.key,
        r.seq,
        r.operation,
        r.payload
    from
        params p
        cross join boundary b
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.seq >= b.seq
        and r.seq <= p.through_seq
        and r.request_id is null
        and r.category in ('prompt', 'context', 'setting', 'tool_catalog', 'tool_schema')
    order by
        r.category, r.key, r.seq desc
),
active as (
    select
        record_id,
        category,
        key,
        seq,
        payload
    from
        latest
    where 1=1
        and operation = 'set'
),
catalog as (
    select
        r.record_id,
        min(r.seq) filter (where r.operation = 'remove') over (
            partition by r.key order by r.seq rows between current row and unbounded following
        ) as removed_at
    from
        params p
        cross join boundary b
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.category = 'tool_catalog'
        and r.seq >= b.seq
        and r.seq <= p.through_seq
),
schemas as (
    select
        s.record_id,
        s.payload ->> 'catalog_revision' as catalog_revision
    from
        active s
        join catalog c on c.record_id = s.payload ->> 'catalog_revision'
    where 1=1
        and s.category = 'tool_schema'
        and c.removed_at is null
),
selected as (
    select
        a.record_id
    from
        active a
    where 1=1
        and a.category <> 'tool_schema'
    union
    select
        record_id
    from
        schemas
    union
    select
        catalog_revision
    from
        schemas
)
select
    r.session_id,
    r.seq,
    r.record_id,
    r.kind,
    r.category,
    r.role,
    r.key,
    r.operation,
    r.request_id,
    r.copied_from_id,
    r.created_at,
    r.version,
    r.payload
from
    params p
    join boba.records r on r.session_id = p.session_id
    join selected s on s.record_id = r.record_id
order by
    r.seq;
