-- Execute in the append transaction, before append.sql obtains its snapshot.
with
params as (
    select
        $1::text as session_id
)
select
    pg_advisory_xact_lock(hashtextextended('boba.history.write:' || p.session_id, 0))
from
    params p;
