-- Received input without acceptance or rejection, including input before compaction.
-- The consumer must recheck under the session write lock before recording a decision.
with
params as (
    select
        $1::text as session_id,
        $2::bigint as through_seq
)
select
    i.session_id,
    i.seq,
    i.record_id,
    i.kind,
    i.category,
    i.role,
    i.key,
    i.operation,
    i.request_id,
    i.copied_from_id,
    i.created_at,
    i.version,
    i.payload
from
    params p
    join boba.records i on i.session_id = p.session_id
    left join boba.records a
        on a.session_id = i.session_id
        and a.category = 'admission'
        and a.payload ->> 'source_record_id' = i.record_id
        and (p.through_seq is null or a.seq <= p.through_seq)
where 1=1
    and i.category = 'input'
    and i.copied_from_id is null
    and (p.through_seq is null or i.seq <= p.through_seq)
    and a.seq is null
order by
    i.seq;
