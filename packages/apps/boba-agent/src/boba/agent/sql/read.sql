with
params as (
    select
        $1::text as session_id,
        $2::bigint as after_seq,
        $3::bigint as through_seq
)
select
    to_jsonb(r) - 'scope' || jsonb_strip_nulls(jsonb_build_object('scope', r.scope)) as record
from
    params p
    join boba.records r on r.session_id = p.session_id
where 1=1
    and r.seq > coalesce(p.after_seq, 0)
    and (p.through_seq is null or r.seq <= p.through_seq)
order by
    r.seq;
