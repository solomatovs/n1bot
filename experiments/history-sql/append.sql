-- Run under the session's transaction advisory lock. No stored functions.
with
next_sequence as (
    select
        coalesce(max(r.seq), 0) + 1 as first_seq
    from
        history_sql.records r
    where 1=1
        and r.session_id = %s
),
written as (
    insert into history_sql.records
    select
        r.*
    from
        next_sequence n
        cross join jsonb_array_elements(%s::jsonb) with ordinality x(value, number)
        cross join lateral jsonb_populate_record(null::history_sql.records,
            x.value || jsonb_build_object('session_id', %s::text,
                'seq', n.first_seq + x.number - 1)) r
    order by
        r.seq
    returning
        seq, record_id
)
select
    seq, record_id
from
    written
order by
    seq;
