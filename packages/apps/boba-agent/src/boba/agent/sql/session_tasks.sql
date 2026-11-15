-- Latest saved state of every task, including tasks interrupted by a process exit.
with
params as (
    select
        $1::text as session_id
)
select distinct on (r.payload ->> 'task_id')
    r.payload ->> 'task_id' as task_id,
    r.payload #>> '{state,status}' as status
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and r.kind = 'task_state'
order by
    r.payload ->> 'task_id', r.seq desc;
