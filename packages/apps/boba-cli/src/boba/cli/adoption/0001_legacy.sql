do $migration$
begin
    if to_regclass('{schema}.users') is null then
        raise exception 'Cannot adopt boba schema: required legacy table users is missing';
    end if;
    if to_regclass('{schema}.roles') is null then
        raise exception 'Cannot adopt boba schema: required legacy table roles is missing';
    end if;
    if to_regclass('{schema}.user_roles') is null then
        raise exception 'Cannot adopt boba schema: required legacy table user_roles is missing';
    end if;
    if to_regclass('{schema}.session') is null then
        raise exception 'Cannot adopt boba schema: required legacy table session is missing';
    end if;
    if to_regclass('{schema}.session_records') is null then
        raise exception 'Cannot adopt boba schema: required legacy table session_records is missing';
    end if;
    if to_regclass('{schema}.threads') is not null then
        raise exception 'Cannot adopt boba schema: foreign application table threads exists';
    end if;
    if to_regclass('{schema}.mcp_servers') is not null then
        raise exception 'Cannot adopt boba schema: foreign application table mcp_servers exists';
    end if;
    if to_regclass('{schema}.mcp_endpoints') is not null then
        raise exception 'Cannot adopt boba schema: foreign application table mcp_endpoints exists';
    end if;
    if to_regclass('{schema}.connections') is not null then
        raise exception 'Cannot adopt boba schema: foreign application table connections exists';
    end if;
end
$migration$;
