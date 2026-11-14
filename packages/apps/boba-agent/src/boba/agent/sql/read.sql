with
params as (
    select
        $1::text as session_id,
        $2::bigint as after_seq,
        $3::bigint as through_seq
)
select
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
    and r.seq > coalesce(p.after_seq, 0)
    and (p.through_seq is null or r.seq <= p.through_seq)
order by
    r.seq;
