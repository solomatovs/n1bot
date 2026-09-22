select
    name,
    create_query,
    hex(sipHash64(tuple(name, create_query))) as row_version
from
    system.functions
where
    origin = 'SQLUserDefined'
