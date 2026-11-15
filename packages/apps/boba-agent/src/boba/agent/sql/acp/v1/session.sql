with
params as (
    select
        $1::text as session_id,
        $2::jsonb as options
),
mode as (
    select
        o.value
    from
        params p
        cross join jsonb_array_elements(p.options -> 'configOptions') o(value)
    where 1=1
        and o.value ->> 'id' = 'mode'
),
mode_state as (
    select
        jsonb_build_object(
            'currentModeId', m.value -> 'currentValue',
            'availableModes', (
                select
                    jsonb_agg(jsonb_build_object(
                        'id', c.value -> 'value',
                        'name', c.value -> 'name'
                    ) order by c.number)
                from
                    jsonb_array_elements(m.value -> 'options') with ordinality c(value, number)
            )
        ) as value
    from
        mode m
),
setup as (
    select
        jsonb_build_object('configOptions', p.options -> 'configOptions') as configured,
        jsonb_strip_nulls(jsonb_build_object(
            'configOptions', p.options -> 'configOptions',
            'modes', (select value from mode_state)
        )) as response
    from
        params p
)
select
    s.response as setup,
    s.configured,
    jsonb_build_object('sessionId', p.session_id) || s.response as created,
    (
        select
            jsonb_build_object(
                'sessionUpdate', 'current_mode_update',
                'currentModeId', m.value -> 'currentValue'
            )
        from
            mode m
    ) as mode_update
from
    params p
    cross join setup s;
