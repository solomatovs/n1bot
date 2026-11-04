do $migration$
begin
    if to_regclass('{schema}.users') is null then
        raise exception 'Cannot adopt mcp schema: required legacy table users is missing';
    end if;
    if to_regclass('{schema}.roles') is null then
        raise exception 'Cannot adopt mcp schema: required legacy table roles is missing';
    end if;
    if to_regclass('{schema}.user_roles') is null then
        raise exception 'Cannot adopt mcp schema: required legacy table user_roles is missing';
    end if;
    if to_regclass('{schema}.connections') is null then
        raise exception 'Cannot adopt mcp schema: required legacy table connections is missing';
    end if;
    if to_regclass('{schema}.grants') is null then
        raise exception 'Cannot adopt mcp schema: required legacy table grants is missing';
    end if;
    if to_regclass('{schema}.threads') is not null then
        raise exception 'Cannot adopt mcp schema: foreign application table threads exists';
    end if;
    if to_regclass('{schema}.session') is not null then
        raise exception 'Cannot adopt mcp schema: foreign application table session exists';
    end if;
    if to_regclass('{schema}.mcp_servers') is not null then
        raise exception 'Cannot adopt mcp schema: foreign application table mcp_servers exists';
    end if;
end
$migration$;
