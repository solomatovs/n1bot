with
params as (
    select
        $1::text as session_id
)
select
    to_jsonb(r) as record
from
    params p
    join boba.sessions s on s.session_id = p.session_id
    join boba.records r on r.session_id = s.session_id
where 1=1
    and r.kind = 'user_message'
    and r.scope = 'session'
    and r.copied_from_id is null
    and r.seq > s.processed_input_seq
order by
    r.seq;
