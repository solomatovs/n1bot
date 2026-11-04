create or replace function {schema}.check_mcp_grant() returns trigger language plpgsql as $body$
begin
    if new.tgt_kind = 'users' then
        if new.src_kind <> 'connections' then
            raise exception 'Only connections can be granted directly to users';
        end if;
        perform 1 from {schema}.users where id = new.tgt_kind_id for key share;
        if not found then
            raise exception 'Grant target user does not exist';
        end if;
    elsif new.tgt_kind = 'roles' then
        perform 1 from {schema}.roles where id = new.tgt_kind_id for key share;
        if not found then
            raise exception 'Grant target role does not exist';
        end if;
    else
        raise exception 'MCP grant target must be users or roles';
    end if;

    if new.src_kind = 'connections' then
        perform 1 from {schema}.connections where id = new.src_kind_id for key share;
        if not found then
            raise exception 'Grant connection does not exist';
        end if;
    elsif new.src_kind = 'mcp_endpoints' then
        perform 1 from {schema}.mcp_endpoints where id = new.src_kind_id for key share;
        if not found then
            raise exception 'Grant endpoint does not exist';
        end if;
    else
        raise exception 'MCP grant source must be connections or mcp_endpoints';
    end if;
    return new;
end
$body$;

create constraint trigger check_mcp_grant
    after insert or update on {schema}.grants
    deferrable initially immediate
    for each row execute function {schema}.check_mcp_grant();

update {schema}.grants set id = id;

create or replace function {schema}.remove_mcp_grants() returns trigger language plpgsql as $body$
begin
    delete from {schema}.grants
    where (src_kind = tg_table_name and src_kind_id = old.id)
       or (tgt_kind = tg_table_name and tgt_kind_id = old.id);
    return old;
end
$body$;

create trigger remove_endpoint_grants after delete on {schema}.mcp_endpoints
    for each row execute function {schema}.remove_mcp_grants();
create trigger remove_connection_grants after delete on {schema}.connections
    for each row execute function {schema}.remove_mcp_grants();
create trigger remove_user_grants after delete on {schema}.users
    for each row execute function {schema}.remove_mcp_grants();
create trigger remove_role_grants after delete on {schema}.roles
    for each row execute function {schema}.remove_mcp_grants();

create or replace function {schema}.mcp_endpoint_updated() returns trigger language plpgsql as $body$
begin
    new.updated_at = now();
    return new;
end
$body$;
create trigger mcp_endpoint_updated before update on {schema}.mcp_endpoints
    for each row execute function {schema}.mcp_endpoint_updated();
