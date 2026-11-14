-- Background state not yet admitted to the LLM context; consumed only between messages.
with
params as (
    select
        $1::text as session_id
),
states as (
    select distinct on (r.payload ->> 'task_id')
        r.seq,
        r.payload ->> 'task_id' as task_id,
        to_jsonb(r) as record
    from
        params p
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.kind = 'task_state'
        and r.payload #>> '{state,execution_mode}' = 'background'
    order by
        r.payload ->> 'task_id', r.seq desc
),
consumed as (
    select
        r.key,
        max(r.seq) as seq
    from
        params p
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.kind in ('task_context', 'task_acknowledged')
    group by
        r.key
)
select
    s.record
from
    states s
    left join consumed c on c.key = s.task_id
where 1=1
    and s.seq > coalesce(c.seq, 0)
order by
    s.seq;
