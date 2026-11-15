-- Copy a stable history under the source session's write and execution locks.
with
params as (
    select
        $1::text as source_session_id,
        $2::text as session_id,
        $3::text as owner_id,
        $4::text as cwd
),
created as (
    insert into boba.sessions (session_id, owner_id, cwd)
    select
        p.session_id,
        p.owner_id,
        p.cwd
    from
        params p
    returning
        session_id
),
copied as (
    insert into boba.records (
        session_id, seq, record_id, kind, category, role, key, operation,
        request_id, copied_from_id, created_at, version, payload
    )
    select
        c.session_id,
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
        cross join created c
        join boba.records r on r.session_id = p.source_session_id
    order by
        r.seq
    returning
        seq
)
select
    max(seq) as through_seq
from
    copied;
