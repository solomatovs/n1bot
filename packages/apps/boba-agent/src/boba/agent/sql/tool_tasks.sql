with
params as (
    select
        $1::text as session_id,
        $2::text as task_id
),
calls as (
    select distinct on (r.key)
        r.session_id,
        r.key,
        r.seq,
        to_jsonb(r) - 'scope' as record
    from
        params p
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.category = 'tool_call'
        and r.payload ->> 'task_id' is not null
        and (p.task_id is null or r.payload ->> 'task_id' = p.task_id)
    order by
        r.key,
        r.seq desc
)
select
    c.record as call,
    result.record as result
from
    calls c
    left join lateral (
        select
            to_jsonb(r) - 'scope' as record
        from
            boba.records r
        where 1=1
            and r.session_id = c.session_id
            and r.category = 'tool_result'
            and r.key = c.key
        order by
            r.seq desc
        limit
            1
    ) result on true
order by
    c.seq;
