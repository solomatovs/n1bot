-- Session state; request-scoped values are selected by openai.sql for an operation.
with
params as (
    select
        $1::text as session_id,
        $2::text as category,
        $3::text[] as kinds,
        $4::text as key,
        $5::bigint as through_seq
)
select distinct on (r.category, coalesce(r.key, r.kind))
    to_jsonb(r) - 'scope' || jsonb_strip_nulls(jsonb_build_object('scope', r.scope)) as record
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and (p.category is null or r.category = p.category)
    and (p.kinds is null or r.kind = any(p.kinds))
    and (p.key is null or r.key = p.key)
    and (p.through_seq is null or r.seq <= p.through_seq)
    and r.scope is distinct from 'request'
order by
    r.category, coalesce(r.key, r.kind), r.seq desc;
