with
params as (
    select
        $1::text as session_id,
        $2::text as call_record_id
)
select
    to_jsonb(c) - 'scope' as call,
    to_jsonb(a) as assistant
from
    params p
    join boba.records c
        on c.session_id = p.session_id
        and c.record_id = p.call_record_id
    join boba.records a
        on a.session_id = c.session_id
        and a.record_id = c.payload ->> 'assistant_record_id'
where 1=1
    and c.category = 'tool_call'
    and a.kind = 'assistant_message';
