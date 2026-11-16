with
params as (
    select
        $1::text as session_id
)
select distinct on (r.key)
    r.key as call_id,
    r.payload ->> 'status' as status
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and r.category = 'tool_result'
order by
    r.key,
    r.seq desc;
