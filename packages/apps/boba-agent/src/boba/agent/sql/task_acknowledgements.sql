with
params as (
    select
        $1::text as session_id,
        $2::text as request_id
),
context as (
    select distinct on (r.key)
        r.key,
        r.kind,
        r.payload
    from
        params p
        join boba.records r on r.session_id = p.session_id
    where 1=1
        and r.kind in ('task_context', 'task_acknowledged')
    order by
        r.key, r.seq desc
)
select
    c.key as task_id,
    n.record_id as notification_id
from
    params p
    join boba.records q on q.session_id = p.session_id and q.record_id = p.request_id
    join boba.records n on n.session_id = p.session_id and n.seq < q.seq
    join context c on c.key = n.payload ->> 'task_id'
where 1=1
    and n.kind = 'task_notification'
    and c.kind = 'task_context'
    and c.payload ->> 'related_message_id' = n.payload ->> 'related_message_id'
order by
    n.seq;
