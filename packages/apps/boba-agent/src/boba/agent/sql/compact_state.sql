-- Persistent state copied after a successful summary. Include referenced catalog revisions.
with
params as (
    select
        $1::text as session_id,
        $2::bigint as through_seq,
        $3::bigint as retain_after_seq
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
        and r.scope is distinct from 'request'
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
results as materialized (
    select distinct on (r.key)
        r.record_id,
        r.seq,
        r.payload
    from
        params p
        cross join boundary b
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.category = 'tool_result'
        and r.seq >= b.seq
        and r.seq <= p.through_seq
    order by
        r.key,
        r.seq desc
),
messages as (
    select
        r.record_id
    from
        params p
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.category = 'message'
        and r.scope = 'session'
        and r.seq > p.retain_after_seq
        and r.seq <= p.through_seq
    union
    select
        c.payload ->> 'assistant_record_id'
    from
        params p
        cross join results r
        join boba.records c
            on c.session_id = p.session_id
            and c.record_id = r.payload ->> 'call_record_id'
    where 1=1
        and (r.payload ->> 'status' in ('pending', 'running') or r.seq > p.retain_after_seq)
),
calls as (
    select
        c.record_id
    from
        params p
        join boba.records c on c.session_id = p.session_id
        join messages m on m.record_id = c.payload ->> 'assistant_record_id'
    where 1=1
        and c.category = 'tool_call'
        and c.seq <= p.through_seq
),
kept_results as (
    select
        r.record_id,
        r.payload ->> 'source_record_id' as source_record_id
    from
        results r
        join calls c on c.record_id = r.payload ->> 'call_record_id'
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
    union
    select
        record_id
    from
        messages
    union
    select
        record_id
    from
        calls
    union
    select
        record_id
    from
        kept_results
    union
    select
        source_record_id
    from
        kept_results
    where 1=1
        and source_record_id is not null
)
select
    to_jsonb(r) - 'scope' || jsonb_strip_nulls(jsonb_build_object('scope', r.scope)) as record
from
    params p
    join boba.records r on r.session_id = p.session_id
    join selected s on s.record_id = r.record_id
order by
    r.seq;
