with
params as (
    select
        $1::text as session_id,
        $2::text as kind,
        $3::text as key
)
select
    to_jsonb(r) - 'scope' || jsonb_strip_nulls(jsonb_build_object('scope', r.scope)) as record
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and r.kind = p.kind
    and r.key is not distinct from p.key
    and r.scope is distinct from 'request'
order by
    r.seq desc
limit
    1;
