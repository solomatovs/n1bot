drop table if exists {schema}.mcp_endpoint_tools;
drop table if exists {schema}.role_tools;
alter table {schema}.mcp_endpoints drop column if exists connections;
