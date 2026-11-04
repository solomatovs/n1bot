do $migration$
begin
    if to_regclass('{schema}.users') is null then
        raise exception 'Cannot adopt aid schema: required legacy table users is missing';
    end if;
    if to_regclass('{schema}.roles') is null then
        raise exception 'Cannot adopt aid schema: required legacy table roles is missing';
    end if;
    if to_regclass('{schema}.user_roles') is null then
        raise exception 'Cannot adopt aid schema: required legacy table user_roles is missing';
    end if;
    if to_regclass('{schema}.threads') is null then
        raise exception 'Cannot adopt aid schema: required legacy table threads is missing';
    end if;
    if to_regclass('{schema}.mcp_servers') is null then
        raise exception 'Cannot adopt aid schema: required legacy table mcp_servers is missing';
    end if;
    if to_regclass('{schema}.session') is not null then
        raise exception 'Cannot adopt aid schema: foreign application table session exists';
    end if;
    if to_regclass('{schema}.mcp_endpoints') is not null then
        raise exception 'Cannot adopt aid schema: foreign application table mcp_endpoints exists';
    end if;
    if to_regclass('{schema}.connections') is not null then
        raise exception 'Cannot adopt aid schema: foreign application table connections exists';
    end if;
end
$migration$;
