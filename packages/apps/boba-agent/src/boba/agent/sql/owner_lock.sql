with
params as (
    select
        $1::text as session_id
)
select
    pg_try_advisory_lock(hashtextextended('boba.agent.owner:' || session_id, 0)) as acquired
from
    params;
