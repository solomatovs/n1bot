with
params as (
    select
        $1::text as session_id,
        $2::jsonb as options,
        $3::jsonb as commands
),
setup as (
    select
        jsonb_build_object(
            'configOptions', p.options -> 'configOptions',
            'availableCommands', p.commands -> 'availableCommands'
        ) as response
    from
        params p
)
select
    s.response as setup,
    jsonb_build_object('configOptions', p.options -> 'configOptions') as configured,
    jsonb_build_object('sessionId', p.session_id) || s.response as created
from
    params p
    cross join setup s;
