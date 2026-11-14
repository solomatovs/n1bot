with
params as (
    select
        $1::text as session_id
)
select
    pg_advisory_unlock(hashtextextended('boba.agent.owner:' || session_id, 0)) as released
from
    params;
