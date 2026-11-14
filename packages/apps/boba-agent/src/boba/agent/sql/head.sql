with
params as (
    select
        $1::text as session_id
)
select
    coalesce(max(r.seq), 0) as seq
from
    params p
    left join boba.records r on r.session_id = p.session_id;
