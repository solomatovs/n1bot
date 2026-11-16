with
params as (
    select
        $1::text as session_id,
        $2::text as assistant_record_id
)
select
    to_jsonb(r) - 'scope' as record
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and r.category = 'tool_call'
    and r.payload ->> 'assistant_record_id' = p.assistant_record_id
order by
    r.operation_position;
