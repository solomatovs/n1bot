with
params as (
    select
        $1::text as session_id,
        $2::text as owner_id,
        $3::text as cwd
)
insert into boba.sessions (session_id, owner_id, cwd)
select
    session_id,
    owner_id,
    cwd
from
    params
returning
    session_id;
