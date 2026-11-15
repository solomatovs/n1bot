-- Run in a transaction AFTER acquiring the session's transaction advisory lock.
-- Validate typed input before SQL; check error and history_integrity.sql before commit.
-- A retry uses the same record IDs, timestamps, version and payload.
with
params as (
    select
        $1::text as session_id,
        $2::jsonb as records,
        $3::bigint as expected_seq
),
incoming as materialized (
    select
        x.number,
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
        cross join jsonb_array_elements(p.records) with ordinality x(value, number)
        cross join lateral jsonb_populate_record(null::boba.records, x.value) r
),
head as (
    select
        coalesce(max(r.seq), 0) as seq
    from
        params p
        join boba.records r on r.session_id = p.session_id
),
existing as materialized (
    select
        i.number,
        r.seq,
        to_jsonb(r) as record,
        (to_jsonb(r) - 'session_id' - 'seq') =
            (to_jsonb(i) - 'number' - 'session_id' - 'seq') as matches
    from
        incoming i
        cross join params p
        join boba.records r
            on r.session_id = p.session_id
            and r.record_id = i.record_id
),
batch as (
    select
        count(1) as size,
        count(distinct record_id) as ids
    from
        incoming
),
found as (
    select
        count(1) as size,
        count(1) filter (where matches) as matches,
        count(distinct seq - number) as positions
    from
        existing
),
decision as (
    select
        f.size > 0 as retry,
        case
            when b.size = 0 then 'empty_batch'
            when b.ids <> b.size then 'record_conflict'
            when f.size > 0 and (f.matches <> b.size or f.positions <> 1) then 'record_conflict'
            when f.size = 0 and (
                select
                    r.seq
                from
                    params p
                    join boba.records r on r.session_id = p.session_id
                where 1=1
                    and r.kind = 'session_deleted'
                limit
                    1
            ) is not null then 'session_deleted'
            when f.size = 0 and p.expected_seq is not null and p.expected_seq <> h.seq then 'stale_history'
        end as error
    from
        params p
        cross join batch b
        cross join found f
        cross join head h
),
written as (
    insert into boba.records (
        session_id, seq, record_id, kind, category, role, key, operation,
        request_id, copied_from_id, created_at, version, payload
    )
    select
        p.session_id, h.seq + i.number, i.record_id, i.kind, i.category,
        i.role, i.key, i.operation, i.request_id, i.copied_from_id,
        i.created_at, i.version, i.payload
    from
        incoming i
        cross join params p
        cross join head h
        cross join decision d
    where 1=1
        and d.error is null
        and not d.retry
    order by
        i.number
    returning
        session_id,
        seq,
        record_id,
        kind,
        category,
        role,
        key,
        operation,
        request_id,
        copied_from_id,
        created_at,
        version,
        payload
),
receipts as (
    select
        w.seq,
        to_jsonb(w) as record
    from
        written w
    union all
    select
        e.seq,
        e.record
    from
        existing e
        cross join decision d
    where 1=1
        and d.error is null
        and d.retry
)
select
    d.error,
    d.retry,
    (
        select
            coalesce(jsonb_agg(record order by seq), '[]'::jsonb)
        from
            receipts
    ) as records
from
    decision d;
