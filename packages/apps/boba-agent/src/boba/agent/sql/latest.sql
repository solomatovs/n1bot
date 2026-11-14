-- Last state per (category, key); null filters mean all values.
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
    r.session_id,
    r.seq,
    r.record_id,
    r.kind,
    r.category,
    r.role,
    r.key,
    r.operation,
    r.request_id,
    r.copied_from_id,
    r.created_at,
    r.version,
    r.payload
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and (p.category is null or r.category = p.category)
    and (p.kinds is null or r.kind = any(p.kinds))
    and (p.key is null or r.key = p.key)
    and (p.through_seq is null or r.seq <= p.through_seq)
order by
    r.category, coalesce(r.key, r.kind), r.seq desc;
