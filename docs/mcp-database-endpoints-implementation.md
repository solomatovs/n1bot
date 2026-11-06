# MCP endpoint catalog and access control: implementation brief

## 1. Goal

Move MCP endpoint publication and tool access policy from TOML to PostgreSQL.
The MCP service owns its endpoint catalog, connections, and the grants that
control access to them. AID authenticates the user and connects to MCP; it does
not decide which MCP endpoint or tool the user may access.

This document is self-contained implementation guidance for this change. It
describes the target behavior, storage model, migration requirements, and
acceptance criteria. It does not authorize changing unrelated authorization
behavior or merging AID, Boba, and MCP application data.

## 2. Ownership and request flow

1. MCP loads the configured plugin packages and builds the tool registry,
   regardless of whether endpoint rows exist.
2. MCP reads enabled endpoint definitions from its database schema. An empty
   endpoint catalog is a valid state: the service starts with no published MCP
   endpoints.
3. MCP authenticates each request locally from the signed token. User roles
   are loaded from the database at sign-in/refresh; endpoint grants come from
   the in-memory catalog loaded at startup and on SIGHUP. Connection grants are queried on each use.
4. An endpoint defines its tool names. MCP returns only those tools for which
   the caller is authorized and enforces the same decision on `tools/call`.
5. AID consumes the resulting MCP tool list. It must not apply role-based or
   tool-grant authorization of its own.

AID may continue protocol adaptation that is not authorization. In
particular, it may honor the existing `com.boba/scope` extension: hide the
server-managed `scope_id` parameter from the model and add the current thread
ID when forwarding the call. This behavior must remain separate from access
checks.

## 3. MCP-owned tables

All tables below are in the MCP application's configured PostgreSQL schema.
Their DDL and migration history belong to the `boba-mcp` application package.
The common migration runner may execute them, but MCP migrations must not
apply AID or Boba application migrations.

### 3.1 `users`

Stores identities known to MCP.

| Column | Meaning |
|---|---|
| `id uuid primary key` | Stable identity key |
| `identifier text unique not null` | Canonical identity from the authentication layer |
| `created_at timestamptz not null` | Creation time |
| `login text` | Unique canonical user login |

Keep the current identity constraints and case-normalization rules. Do not
store authentication-provider-specific identity logic in endpoint records.

### 3.2 `roles` and `user_roles`

`roles` is the MCP role catalog. It may be empty. Administrators create roles
in the database; authentication providers can assign only roles that already
exist there.

`user_roles` relates a user to a role and retains `managed` so automatic
provider assignment can be distinguished from manual membership. Its keys
reference `users.id` and `roles.id` with cascading deletion.

### 3.3 `grants`

Keep one generic grants table in the MCP schema. It is used for endpoint
visibility and connection access; it does not grant individual tools.

| Column | Meaning |
|---|---|
| `id uuid primary key` | Grant row identity |
| `src_kind text not null` | Resource kind: `mcp_endpoints` or `connections` |
| `src_kind_id uuid not null` | ID of the endpoint or connection |
| `tgt_kind text not null` | `roles` for endpoint access; `users` or `roles` for connections |
| `tgt_kind_id uuid not null` | ID of the target user or role |

Add a uniqueness constraint on `(src_kind, src_kind_id, tgt_kind,
tgt_kind_id)` and an index on `(tgt_kind, tgt_kind_id)`. Because this table is
polymorphic, application writes and migration code must verify that referenced
resources and target users/roles exist. Delete resource grants in the same
transaction as their resource.

An endpoint grant applies to the whole endpoint and its configured tool set.
There is no role-to-tool permission table. If two roles must receive different
tool sets, configure separate endpoints and grant each endpoint to the
appropriate roles.

### 3.4 `connections`

MCP owns named connections. Keep the existing storage shape unless the
implementation discovers a specific requirement that prevents it:

| Column | Meaning |
|---|---|
| `id uuid primary key` | Connection identity |
| `name text not null` | Name passed by tools and shown in the connection catalog |
| `data jsonb not null default '{}'` | Encrypted connection payload and its kind |

Keep the index used to find connection kinds. Connection secrets remain
encrypted with the MCP connection-store key. `grants` rows authorize users or
roles to list, search, and use each connection. MCP must check those grants
when resolving a named connection for every call.

### 3.5 `mcp_endpoints`

One row describes one published MCP URL. The endpoint path is stored in
`prefix_url`; it is separate from MCP's service-level `url_prefix` in
configuration. The route is the concatenation of the service prefix and the
endpoint path. For example, service `url_prefix = "/mcp"` plus endpoint
`prefix_url = "/dq/all"` gives `/mcp/dq/all`. `prefix_url = "/"` publishes
the endpoint at `/mcp/`. The same endpoint path appended
to the configured `public_url` gives the public OAuth resource URL.

| Column | Type / default | Meaning |
|---|---|---|
| `id` | `uuid primary key` | Endpoint identity used by grants |
| `prefix_url` | `text unique not null` | Endpoint path relative to the service prefix, e.g. `/`, `/all`, or `/dq/all` |
| `title` | `text not null` | Human-readable name |
| `description` | `text not null default ''` | Description exposed to MCP clients |
| `instructions` | `text not null default ''` | MCP server instructions for this endpoint |
| `enabled` | `boolean not null default true` | Whether MCP publishes the endpoint |
| `tools` | `jsonb not null default '[]'` | Ordered JSON array of exact tool names |
| `overrides` | `jsonb not null default '{}'` | JSON object: exact tool name to full JSON Schema override |
| `prompts` | `jsonb not null default '{}'` | Endpoint prompt definitions, preserving current prompt behavior |
| `created_at` | `timestamptz not null default now()` | Creation time |
| `updated_at` | `timestamptz not null default now()` | Last change time |

Enforce `jsonb_typeof(tools) = 'array'` and `jsonb_typeof(overrides) =
'object'`. Validate array element types, duplicate tool names, prompt shape,
and override keys in the application layer. Override keys must be a subset of
`tools`. Do not add a separate `mcp_endpoint_tools` table.

`prefix_url` must be a canonical absolute path within the MCP service: it must
be `/` or start with `/` and contain one or more non-empty path segments. Other
paths have no trailing slash, query, fragment, scheme, or `.`/`..` segment.
The path must be unique in the MCP schema. Nested paths are valid. Do not
store the service-level `url_prefix` inside this column. Join paths without a
duplicate or missing slash:
`effective_path = server.url_prefix.rstrip("/") + endpoint.prefix_url`;
build the public resource URL from `server.public_url.rstrip("/")` in the
same way. There is no separate endpoint `name` field: `id` is the database
identity and `prefix_url` is the endpoint's route identity.

Nested endpoint routes may overlap by prefix (for example, `/dq` and
`/dq/all`). The HTTP dispatcher must select the longest matching endpoint
path before passing the request to its MCP/child routes. Reject exact duplicate
paths after normalization.

Do not add a `connection_sources` field. Endpoint tool selection determines
which connection-aware tools are published. `connection_list` and
`connection_search` are ordinary registered tools and are selected by their
exact names like other tools. A connection grant controls which rows those
tools return and which named connections can be resolved during a call.

## 4. Tool names and schema overrides

`tools` stores names exactly as MCP exports them from the runtime registry.
Do not introduce endpoint aliases or rename plugin tools. The registry may
also contain MCP-owned built-in tools; if endpoint administrators may select
those, their exact exported names follow the same rule.

For every enabled endpoint:

- every `tools` item must exist in the fully assembled MCP registry;
- each name must be unique in the JSON array;
- every `overrides` key must name a tool in that endpoint;
- every override value must be a valid JSON Schema object with an object root,
  as required for MCP tool `inputSchema`;
- a bad reference or schema must produce an actionable endpoint/tool error;
  MCP must not silently publish a partial or different tool set.
- `prefix_url` must pass the path validation above and must not collide with
  another endpoint route after joining the service prefix.

When no override exists, publish the schema generated by the plugin/runtime.
When an override exists, it is the endpoint's full model-facing argument
schema. MCP must return that effective schema in `tools/list`; AID then applies
only its declared `scope` adaptation. The server-managed `scope_id` field and
its `x-boba-scope` marker must remain present in the MCP wire schema even when
an override omits it, so AID can hide and later supply it. Administrators do
not configure that reserved field in `overrides`.

MCP remains responsible for valid tool calls. The implementation must define
and enforce compatibility between the override and the plugin's executable
argument model. It must not advertise a schema which the execution path
cannot accept. Validate calls against the published override and retain the
plugin's typed validation. Reject an incompatible enabled endpoint with a
clear error rather than silently changing the schema or relying on repeated
tool-call failures. Evaluate existing dependencies before adding a JSON Schema
validator.

The base schema for administrators comes from the fully assembled MCP runtime
registry and `tools/list`, not a hand-maintained schema copy. `tools/list` is
the MCP contract. Preserve any endpoint audience rules for interface-only
tools as presentation metadata; they are not authorization grants.

## 5. Access rules

- A user sees and can call an endpoint if at least one role in their signed
  session has a `grants` row in the loaded endpoint catalog. Roles originate
  in the database at sign-in/refresh; JWT verification does not read users.
  Disabled endpoints are unavailable regardless of grants.
- MCP applies endpoint authorization on both listing and calls. A caller must
  not call an unlisted endpoint or a tool outside that endpoint's `tools` set.
- Endpoint grants target roles. Do not add a parallel `endpoint_roles` table
  or a `role_tools` table.
- A user may see an endpoint but still have no usable connection rows. Each
  connection-list/search and connection-resolution path independently checks
  `grants` for the connection.
- If a user has no roles, they receive no role-granted endpoints. The service
  still starts and handles health checks normally.
- AID must not repeat any of these checks. It forwards the authenticated
  user's identity/credentials as currently required and uses the tools MCP
  returns.

## 6. Endpoint lifecycle and config

MCP's TOML config retains process-level configuration such as listen address,
PostgreSQL access, authentication methods, OAuth clients, storage, and plugin
configuration. Remove endpoint definitions and tool permission policy from
TOML after the database catalog is in use.

The plugin registry is built even when `mcp_endpoints` is empty. No endpoint
rows means no MCP endpoint URLs are published. Creating an enabled row and its
role grants publishes the configured endpoint after SIGHUP. Startup and
SIGHUP use the same reload operation: read the catalog and grants, prepare
HTTP/OAuth routes and MCP servers, then replace the in-memory catalog.
A failed reload retains the last working catalog and logs the error. There
is no periodic polling, notification listener, or separate reload CLI command.

Verification handoff for signal-driven reload (not executed in this code phase):

- Empty startup, then SQL insert + commit + SIGHUP: health, MCP route and OAuth
  discovery expose the endpoint; authenticated tool listing uses its grants.
- Update/delete/disable endpoints and grants, including prompts and overrides:
  subsequent requests use the replacement cache and removed access is denied.
- Invalid tool/schema or failed SQL: log the failure, preserve the working
  catalog and allow a later successful reload.
- Repeated HUP while loading: serialize reloads and coalesce pending requests;
  TERM/INT during reload: cancel preparation without publishing a partial cache.
- Calls in flight survive reload, subscriptions close, old lifespan resources
  drain, and the process-wide concurrency limit is not reset by reload.
- Existing OAuth tokens/registrations remain usable for retained resources;
  clients explicitly refresh their tool lists after changes.
- TERM/INT and repeated stop signals in MCP, AID, Boba HTTP and Boba stdio;
  previous signal handlers are restored after the owning context closes.
- Overlapping signal owners and re-entry are rejected without overwriting
  installed handlers. Replacement by another owner during the context is
  reported on exit without restoring over that owner's handler. Queued events
  are ignored after the owning receiver is detached.
- Shutdown during catalog read or endpoint preparation: discard the candidate
  instead of publishing it after shutdown has begun. Calls from another event
  loop are rejected. Preparation cancellation preserves cleanup failures.
- Adapt test assembly to `McpEndpoints` accepting the shared
  `RunLimitMiddleware`, and to `McpRuntime` owning an endpoint repository.

Preserve existing prompt behavior by transferring prompt definitions into the
endpoint's `prompts` JSON field. Do not preserve the old endpoint-level role
rules for offering connection sources: endpoint grants and connection grants
replace that policy.

## 7. Migration ownership and rollout

Each application owns its SQL definitions: `boba.aid`, `boba.cli`, and
`boba.mcp_server`. MCP also requests migration plans from enabled plugins;
plugins own their SQL and use the same executor. MCP must not import plugin
storage implementations or apply AID/Boba business tables.

Each version is a complete directory `migrations/<version>/*.sql`. The common
Python launcher executes the selected directory in filename order, every time.
It has no applied-version ledger, checksums, table inventory, or assumptions
about the previous schema. By default it selects the last directory in lexical
order. It never chains earlier version directories.

SQL creates missing declared tables and reconciles their columns, types,
defaults, constraints, comments, indexes and owned sequences. Data conversions
within those tables belong to that same version's SQL. Valid identities,
connection IDs and grants are preserved. Incompatible data causes rollback
with a diagnostic; migration must not silently discard rows to satisfy DDL.

**Only tables declared by this migration are in scope.** Other tables must
remain untouched, including old `role_tools`, `mcp_endpoint_tools`, or
`schema_migrations` if someone has them in the target schema. Do not enumerate
and drop supposedly obsolete tables; do not infer a previous version from them.
This explicit requirement supersedes earlier cleanup proposals. Current clean
installs do not create those tables, and runtime does not use them.

Migration changes storage structure only. It does not import endpoints,
users or grants, map old identities, or derive new values from stored rows.
Endpoint catalog administration is separate from the migrate command.

Each component plan runs in its own transaction and schema advisory lock.
Identical targets are deduplicated for shared plugin storage. Earlier committed
components remain committed if a later plugin fails; retry reruns idempotent
SQL. There is no cross-database transaction. Admins select configured targets
or override database/schema for all targets of one invocation.

## 8. Implementation order

1. Inventory MCP-owned tables, current endpoint TOML, endpoint OAuth resource
   construction, tool registry assembly, and all `role_tools` references.
2. Define complete per-application SQL version directories and a shared
   SQL-agnostic launcher without changing runtime behavior yet.
3. Add MCP SQL for the endpoint catalog and declared-table data conversion.
   Exclude `role_tools` from fresh schemas; existing undeclared tables stay
   untouched.
4. Add an MCP endpoint repository that loads endpoint rows, role grants,
   prompts, tools, and overrides and validates them against loaded plugins.
5. Build OAuth resources/routes and `tools/list` from the database catalog.
   Enforce endpoint grants and tool membership on both listing and calls.
6. Remove MCP endpoint and tool policy from TOML. Remove AID tool/role grant
   checks while retaining identity forwarding and the negotiated scope
   adaptation.
7. Update deployment and compose/debug configs to match the new process-only
   MCP config.
8. At the migration/deployment stage, apply AID, Boba, and MCP migrations to
   their own dev schemas and verify declared-table convergence, preservation
   of valid data, and preservation of undeclared tables.

Test adaptation is a separate follow-up phase after source implementation, as
requested. That phase must cover migrations, authorization and endpoint
visibility, MCP protocol `tools/list`/`tools/call`, schema overrides,
connection grants, and AID scope forwarding.

## 9. Acceptance criteria

- A clean MCP schema contains MCP-owned users, roles, user_roles, grants,
  connections, and `mcp_endpoints`; it contains no `role_tools` or
  `mcp_endpoint_tools`.
- A clean AID schema contains no `role_tools`. Runtime has no AID tool ACL;
  existing undeclared tables are outside migration scope.
- MCP starts successfully with an empty role catalog and an empty endpoint
  catalog; it loads configured plugins but publishes no endpoint URL.
- An enabled endpoint with a valid tool list and role grant appears only to
  authorized callers and returns exactly its effective tools in `tools/list`.
- A call to a disabled, ungranted, or unlisted endpoint/tool is rejected by
  MCP, even if the caller bypasses AID and sends `tools/call` directly.
- A tool's override is the `inputSchema` presented for that endpoint. The
  reserved scope parameter remains server-managed on MCP's wire and is
  correctly hidden/injected by AID for model calls.
- Connection discovery and use are controlled by per-connection grants in
  MCP; there is no endpoint `connection_sources` setting.
- AID performs no role-based or tool-grant authorization for MCP tools.
- Clean installation and in-place migration both complete through each
  application's own `migrate` command. Legacy data is preserved where
  applicable. Migration does not inspect or delete undeclared legacy tables.
- No tests are rewritten during source implementation; a distinct test
  adaptation and full verification phase follows.

## 10. Current code locations to change

This map records the entry points before implementation. Section 14 lists
the resulting components and the separate verification handoff.

These are the implementation entry points in the current repository. The
implementer must search for all callers before removing a class or package;
the list is a map, not permission to delete a shared abstraction that another
application still uses.

### MCP startup and endpoint catalog

- `packages/apps/boba-mcp/src/boba/mcp_server/app.py`
  - `McpSection` and `McpAppConfig`: remove endpoint records from process
    config while retaining server, auth, database, storage, OAuth-client, and
    plugin settings.
  - `mcp_pool`, `mcp_users`, and `mcp_connections`: keep MCP-scoped
    persistence providers; wire in the endpoint repository and MCP-owned
    grants.
  - `McpHost.__init__` and `McpHost.serve`: load plugins independently of
    endpoint rows, read/validate the DB catalog before listening, install the
    catalog and policy, allow an empty catalog, and construct OAuth resources
    from enabled DB endpoints.
  - `McpHost._auth`: derive OAuth protected resources from the DB catalog.
    Build each resource URL as `server.public_url.rstrip("/") +
    endpoint.prefix_url`; do not use a short endpoint key or append the
    service `url_prefix` twice.
  - `McpEntry.run` (`ProgramCommand.MIGRATE`): apply only MCP-owned
    application migrations plus plugin-owned migrations.
- `packages/apps/boba-mcp/src/boba/mcp_server/endpoints.py`
  - `McpEndpointStore` currently joins `mcp_endpoint_tools`, `roles`, and
    `grants`. Change the repository to read `tools` JSON and `overrides` from
    `mcp_endpoints`, join endpoint role grants, and return a typed endpoint
    definition. It must not make a second authorization decision or expose
    raw SQL rows to the HTTP layer.
- `packages/apps/boba-mcp/src/boba/mcp_server/server.py`
  - `McpEndpoint`: replace the TOML `ProfileGrant` model with the typed
    endpoint record needed by the runtime; use `prefix_url` as its route key.
  - `ConnectionOfferRule` and `McpEndpoint.connections` / `offer()`: remove
    role-based connection-source offer policy. Tool selection plus grants on
    individual connection rows are the policy.
  - `RoleToolServers`: use the endpoint's configured tool set and registry;
    remove global `role_tools` lookups. Retain the `scope` argument
    construction required by MCP/AID.
  - `EndpointCatalog`: replace config-backed role/profile catalog behavior
    with endpoint access derived from MCP users' current DB roles and grants,
    keyed by `prefix_url`, or remove it if the existing OAuth resource flow
    can use a smaller explicit port.
  - `McpEndpoints`: retain HTTP/MCP route dispatch and lifecycle ownership;
    construct routes only for enabled DB endpoints and handle the empty case.
    Compose each route from configured `server.url_prefix` and DB
    `prefix_url`; dispatch overlapping nested routes by longest match.
- `packages/apps/boba-mcp/src/boba/mcp_server/auth.py` and
  `packages/services/boba-oauth/src/boba/oauth/server.py`: inspect resource
  and token-scope construction. Keep OAuth implementation generic; pass it
  the MCP endpoint resources instead of TOML names.

### MCP access, plugins, and connections

- `packages/infra/auth/boba-access-postgres/src/boba/access_postgres/roles.py`
  and `packages/infra/auth/boba-access-postgres/src/boba/access_postgres/schema/20_role_tools.sql`:
  remove `RoleToolsTable` and the `role_tools` DDL.
- `packages/core/boba-access/src/boba/access/__init__.py`: remove the
  `RolePermissions` / `role_permissions` bridge and its async per-role tool
  lookup path if repository-wide search confirms no remaining consumer.
  Retain shared access abstractions still used by unrelated applications.
- `packages/services/boba-toolrun/src/boba/toolrun/plugins.py` and
  `packages/apps/boba-mcp/src/boba/mcp_server/app.py`: remove
  plumbing for dynamic role-to-tool permissions from registry assembly where
  it exists solely for MCP; preserve plugin loading and tool construction.
- `packages/apps/boba-mcp/src/boba/mcp_server/connections/store.py`,
  `grants.py`, `tools.py`, `params.py`, and `user_connections.py`: keep the
  per-connection grant check, listing/search, secret decryption, and
  credential preparation in MCP. Remove endpoint-source filtering only after
  confirming there are no other consumers of `ConnectionParams.offered()` or
  `OfferedSource`.
- `packages/apps/boba-mcp/pyproject.toml`,
  `packages/apps/boba-aid/pyproject.toml`, and
  `packages/infra/auth/boba-access-postgres/pyproject.toml`: remove
  dependencies/package data that become unused after schema ownership moves
  into app migrations. Do not remove the generic grants runtime if another
  application still imports it.

### AID relay and configuration references

- `packages/apps/boba-aid/src/boba/aid/agent/relay.py`
  - `McpRelay._list_tools`: remove AID authorization decisions based on roles
    or per-tool grants. Continue honoring MCP's returned set and protocol
    audience metadata; retain `ScopeFeature` schema pruning.
  - `McpRelay._called` / `_called` helper: remove role/tool ACL checks and
    continue forwarding only tools MCP exposed. Retain scope injection and
    protocol error handling.
- `packages/apps/boba-aid/src/boba/aid/infra/entry.py`:
  select only AID's SQL version directory; do not apply shared role-tool DDL
  or cleanup of undeclared tables.
- `docs/admin-guide.md` and `docs/developer-guide.md`: replace examples and
  descriptions of `[mcp.endpoints.*]`, role tool grants, and
  `[[mcp.endpoints.*.connections]]` with DB-managed endpoint instructions.
- `compose/mcp/docker-compose.yml` and the deployment/debug config sources
  used to generate mounted MCP TOML: remove endpoint and role-tool policy
  from the service config while keeping process configuration. The runtime
  `compose/mcp/conf` files are deployment data and may not be tracked in this
  repository; update their tracked source/template rather than relying on an
  ignored local copy.

### Migration implementation

- `packages/infra/db/boba-db-postgres/src/boba/db/postgres/migration_cli.py`:
  load a selected complete SQL version directory and coordinate component
  plans through the common transactional executor.
- `packages/apps/boba-aid/src/boba/aid/infra/entry.py`,
  `packages/apps/boba-cli/src/boba/cli/app.py`, and
  `packages/apps/boba-mcp/src/boba/mcp_server/app.py`: give each application
  only its own migration stream. A shared runner is allowed; cross-application
  table migration plans are not.
- Add MCP-owned schema and migration resources under
  `packages/apps/boba-mcp/src/boba/mcp_server/` and include them as package
  data in `packages/apps/boba-mcp/pyproject.toml`.
- `docs/migrations/2026-11-03-database-roles/` and current migration docs:
  update the maintained instructions to explain app-owned SQL directories
  and preservation of undeclared tables. Historical applied artifacts can
  remain as history, but must not be selected as a clean current schema.

Test adaptation remains separate from the source change. Relevant follow-up
locations include `packages/apps/boba-mcp/tests/service_stand.py`,
`test_server.py`, `test_service_config.py`, `test_empty_roles.py`,
`packages/apps/boba-aid/tests/aid_stand.py`, and migration fixtures that
currently apply `boba.access_postgres`.

## 11. Component boundaries and error ownership

| Component | Owns | Must not own | Errors and caller behavior |
|---|---|---|---|
| MCP endpoint repository (`mcp_server/endpoints.py`) | SQL reads for endpoint rows and grants; maps database rows to typed records | HTTP routes, OAuth policy, plugin execution, deciding user access a second time | Translate PostgreSQL failures to a repository-specific error with the original cause. `McpHost` treats catalog read failure as startup failure; do not start with an empty catalog after a DB error. |
| Endpoint model and validator (`mcp_server/server.py` or a focused domain module) | `prefix_url`, tools, overrides, prompt shape, and cross-reference validation against the assembled registry | SQL, OAuth token creation, low-level ASGI routing | Report invalid route/tool/schema with `prefix_url` and tool. An enabled invalid endpoint fails startup clearly; do not silently skip tools or broaden access. |
| MCP access policy | Use roles from the authenticated session and the loaded DB grant catalog; grant endpoint access; check endpoint tool membership | Pydantic plugin argument validation, database migrations, AID chat policy | Denial remains an authorization/protocol denial at MCP. It must never become “empty tools” when the reason is a database or authentication failure. Use the existing OAuth/MCP denial format consistently. |
| Connection broker | Store and decrypt connection data; list/search grants; resolve and authorize each named connection; prepare credentials | Endpoint membership and endpoint role policy | Keep `ConnectionStoreError`, connection refusal, and credential errors distinct. Convert them to MCP tool-call errors at the MCP execution boundary without returning encrypted data or credentials. |
| Plugin/runtime registry | Load configured plugins, create tools, expose their exported names and source schemas | Endpoint publication, role grants, user authentication | Plugin construction/configuration failure is a startup error with plugin context. It must not be reported as an authorization denial. |
| MCP application composition (`McpHost`) | Wire config, DB repositories, auth, plugins, endpoint policy, and HTTP server | Reimplementing SQL grant queries or plugin-specific validation | Startup errors stop the process and are reported by the CLI with their cause. Do not catch broad exceptions and continue with partial endpoint state. |
| MCP protocol/HTTP layer (`McpEndpoints`, `McpServer`) | Endpoint routes, OAuth-protected resources, MCP `tools/list` and `tools/call`, HTTP lifecycle | Persisting policy or deciding grants independently of the access policy | Invalid method/request and unauthorized resource use protocol/OAuth errors. Tool execution failures return MCP tool results with `isError`; they must not leak stack traces or secrets. |
| AID MCP relay (`McpRelay`) | Connect using the current user's credentials; pass through the MCP-authorized tools; apply negotiated audience and `scope` protocol adaptations | Role-to-endpoint, role-to-tool, or connection-grant authorization | Preserve authentication, transport, and MCP protocol errors distinctly. Do not turn MCP failure into an empty tool list. AID catches only errors it can translate at its own client/UI boundary. |
| Per-app migration command | Apply the selected SQL directory; MCP also coordinates enabled plugin plans | Serving traffic, inspecting undeclared tables, or owning plugin SQL | Failure rolls back the current component transaction and exits nonzero with component/version/file context; prior completed components remain committed. |

For every new exception, document its owner, what state it guarantees, and
which boundary catches/translates it. Preserve causal chaining. Do not catch
`Exception` merely to return an empty endpoint list or a generic “not
authorized” response.

## 12. Components and structures removed after cutover

Remove these from the active implementation once the database-backed path is
in use:

- Creation and use of `role_tools`, its SQL resource
  `boba.access_postgres/schema/20_role_tools.sql`, and
  `RoleToolsTable` in `boba.access_postgres.roles`.
- The MCP `RolePermissions`/`role_permissions` runtime bridge if the usage
  audit confirms its only remaining purpose was `role_tools`.
- `mcp_endpoint_tools`; its replacement is the JSON `tools` field on
  `mcp_endpoints`.
- TOML endpoint model/config path `McpSection.endpoints` and
  `[mcp.endpoints.<name>]` definitions.
- Endpoint-level `roles`/`tools` policy embedded in `ProfileGrant` once the
  DB endpoint model is wired. Role authorization belongs to `grants`; tool
  selection belongs to `mcp_endpoints.tools`.
- `ConnectionOfferRule` and role-based `McpEndpoint.connections` offer rules.
  There is no replacement `connection_sources` column. Individual connection
  grants remain.
- AID's role/tool grant checks for MCP tools. Keep only its MCP client,
  audience/presentation handling, and negotiated scope behavior.
- Seed/config-only endpoint examples and docs that describe the old runtime
  as authoritative. Keep historical migration notes clearly marked as
  historical.

Before deleting shared code (`ProfileGrant`, `ToolAccess` static role grants,
`OfferedSource`, `ConnectionParams.offered`, or `boba-access-postgres` as a
package), use repository-wide reference search and retain any part still owned
by another application. “Remove role_tools” does not authorize deleting the
generic `grants` model or per-connection authorization.

## 13. Optional code-size and dependency reduction

The implementation agent must look for reduction opportunities but must not
make them mandatory scope or perform broad replacements just to reduce line
count. For each candidate, record:

- current code/package and concrete consumers;
- proposed deletion, consolidation, or existing library that could replace it;
- behavior and compatibility coverage;
- direct and transitive dependency impact, including duplicated libraries,
  native/binary packages, and SAST/SCA implications;
- estimated code removed and maintenance cost added or removed.

Prefer removing code made unreachable by this refactor, consolidating
duplicate endpoint/policy representations, and using libraries already in
the dependency graph. Do not add a dependency solely for a cosmetic cleanup.
Do not replace connection, OAuth, or database implementations without
comparing behavior, transitive dependencies, and operational costs. Mark
unrelated opportunities as follow-up candidates rather than expanding this
change.


## 14. Implemented source phase — 2026-11-04

Source changes are ready for the separate test-adaptation agent. No tests or
fixtures were edited or executed in this phase. Earlier source-only pyright,
ruff and `sec/sec.sh sast` runs succeeded; the final source state still needs
verification in the next phase. No Docker rollout or migration of the live
`aid` / `aid_ss` databases was performed in this phase.

### Actual component map

| Component | Implementation |
|---|---|
| Endpoint and prompt records; endpoint access | `packages/apps/boba-mcp/src/boba/mcp_server/catalog.py`: `McpEndpoint`, `PromptFile`, `EndpointCatalog`; no SQL or FastMCP imports |
| Endpoint repository | `packages/apps/boba-mcp/src/boba/mcp_server/endpoints.py`: `McpEndpointStore`; database errors become `EndpointStoreError` |
| Schema overrides | `packages/apps/boba-mcp/src/boba/mcp_server/schemas.py`: `EndpointSchemas`, `DescribedTool`; configuration failures become `EndpointConfigError`, invalid calls become `ToolError` |
| HTTP, tools and nested routes | `packages/apps/boba-mcp/src/boba/mcp_server/server.py`: `EndpointTools`, `McpServer`, `McpEndpoints` |
| Composition and CLI | `packages/apps/boba-mcp/src/boba/mcp_server/app.py`: `McpHost`, `McpEntry`; commands `serve`, `health`, `migrate`, `tools` |
| Stateless resource authorization | `packages/services/boba-oauth/src/boba/oauth/server.py`: optional `ProfileCatalog` in `ServiceAuth` / `ResourceTokens`; applies to the complete resource, including files/prompts |
| Common execution policy port | `packages/core/boba-access/src/boba/access/__init__.py`: `ToolPolicy`; applied by `ToolAccessGuard`, injected by runtime |
| Connection parameter publication | `packages/apps/boba-mcp/src/boba/mcp_server/connections/params.py`: `published()`; no source-offer filtering |
| Shared migration execution | `packages/infra/db/boba-db-postgres/src/boba/db/postgres/migrate.py`: versioned `MigrationPlan`, `MigrationScripts.application()`, `PostgresMigrations` |
| Migration composition | `packages/infra/db/boba-db-postgres/src/boba/db/postgres/migration_cli.py`: `ApplicationMigrations` and target overrides |
| SQL reconciliation | `packages/infra/db/boba-db-postgres/src/boba/db/postgres/sql/10_reconcile.sql`: temporary model comparison; declared tables only |

Application SQL is in `migrations/0001/` under `boba.aid`, `boba.cli`,
`boba.mcp_server`. Plugin storage has the same layout in `boba.db.pgvector`
and `boba.tool.describer`. Shared temporary SQL helpers compare only declared
tables with their temporary models. No ledger, manifests, adoption directory,
or cleanup of undeclared tables remains in this mechanism. Old testing support
still calls removed migration APIs/resources and needs separate adaptation.

The implementation removes `RoleToolsTable`, its DDL, `RolePermissions`,
`role_permissions`, `RoleTools` and `RoleToolServers`, `ConnectionOfferRule`,
`OfferedSource`, `ConnectionParams.offered()`, and MCP endpoint TOML config.
`McpToolProvider` now returns exactly the application's tool set; it has no
parallel unconditional-operations list. AID relay was inspected: it already
has no role/tool ACL, so its audience/scope adaptation was retained.

### Operational details for the next agent

- `prefix_url` is a relative service route starting with `/`, such as `/`
  for the service root or `/dq/all` for a nested route. `[server].url_prefix`
  remains the service prefix. OAuth scopes
  now use endpoint paths, so old OAuth sessions need reauthentication.
- Endpoint catalog and grants are loaded at startup and on SIGHUP. Reload
  updates HTTP routes, OAuth resources/discovery, tool schemas and prompts.
  Existing requests drain on the previous application; subscriptions are
  ended, and subsequent requests use the new catalog. Clients must request
  their tool list again; reload does not push new tools into agent context.
  User-role changes still take effect at sign-in/refresh; there are no DB
  reads during JWT verification. Connection grants remain per-call DB checks.
  Process configuration, loaded plugins and model weights are not reloaded.
- Overrides contain a full model-facing inputSchema. This version allows
  changes only to annotations (`title`, `description`, `examples`, `$comment`,
  `deprecated`, `readOnly`, `writeOnly`). Validation keywords, defaults and
  server extensions must match. This is the explicit compatibility rule;
  broader schema transformations require a separately designed contract.
- `workflow` and `file_upload` are explicit tool names in the endpoint list.
  `workflow` is generated by runtime when the endpoint includes streaming
  tools; its nodes cannot call tools outside the endpoint's port.
- `boba-mcp tools --config ...` writes a JSON mapping by exported tool name:
  `mcp` is the MCP descriptor, `override` is the editable model schema.
  It loads plugins but does not listen on HTTP or require endpoint rows.
- `boba-mcp migrate --config ...` applies the selected structure to configured
  or explicitly overridden targets. It accepts no data-import document and
  does not populate endpoints or role membership.
- Package-data declarations include app SQL directories. The existing Make /
  `boba.core.package_data` inventory consumes them; no separate SQL-copy build
  step was added. Binary builds and package-resource checks are pending.

### Separate verification and rollout phase

The next agent owns test changes, test execution, and correction of findings.
At minimum, adapt the previously listed test locations and cover:

1. Clean and drifted declared tables, repeated selected-version execution,
   incompatible-data rollback, preservation of undeclared tables and valid
   user/role/connection IDs and grants.
2. Normalized endpoint/prompt input, conflicting repeat input, grant integrity
   and resource deletion. Existing legacy tables must remain untouched.
3. Empty roles and endpoints, plugin startup failure, unknown tool names,
   duplicate names, and disabled or malformed endpoint records.
4. OAuth login/refresh, encrypted Kerberos credentials, and no DB queries in
   token verification; endpoint denial on tools, prompts, files and journals.
5. `/all`, `/dq/all` and overlapping `/dq` routes, service prefixes, discovery
   URLs, resource audiences/scopes, and retired-token reauthentication.
6. Exact `tools/list` membership and direct `tools/call` attempts outside it,
   including `workflow` nodes and the explicit `file_upload` built-in.
7. Schema export, annotation overrides, rejected contract changes, reserved
   scope fields, typed argument validation, and AID scope injection.
8. Connection list/search/use with user and role grants, including revocation.
9. Package wheels/Nuitka resources and Make builds, followed by migrations in
   `aid` and `aid_ss`, restart/deployment, and manual chat review.

Do not treat static analysis as a successful functional or migration test.

### Code and dependency reduction found in this change

Removed the asynchronous role-tool lookup and its cache/plumbing, the
connection-source policy and unconditional MCP operation publication. AID
and MCP no longer depend on `boba-access-postgres`. Direct dependencies on
`jsonschema`, `psycopg`, and `omegaconf` describe actual MCP imports; all three
were already in its dependency graph, so this adds no new external library.
The lockfile was regenerated without upgrading dependency versions.

Optional follow-ups, after test adaptation:

- Retire `boba-access-postgres` and shared legacy schema resources once old
  fixtures use application migration streams. Runtime grants remain in
  `boba.access.grants` and connection storage; those must be preserved.
- Evaluate removing the static `ToolAccess` / `ToolGrants` adapter and unused
  registry selection helpers if test-support consumers can use `ToolPolicy`
  directly. Chat `RoleVisibility` still has real consumers.
- Evaluate consolidating endpoint tool assembly and schema export if further
  built-ins are added. Keep executable schemas owned by runtime and avoid
  copying AID's presentation logic into MCP.

These are opportunities, not requirements for this implementation.

## 15. SQL migration implementation and handoff (2026-11-04)

The current source contract is §7. The previous ownership manifest, version
ledger, checksums, adoption scripts and lists of retired tables were removed.
`MigrationPreparation`, `McpMigrationInput`, and the Python pgvector index
builder were removed too. Migration code must not grow a registry of apps,
plugins or their tables. Apps supply a package name; plugins supply SQL plans.

`MigrationScripts.application(package, version=...)` loads the selected folder
and generic SQL helpers. Plans carry unrendered SQL and typed substitutions:
identifiers use `sql.Identifier`, values use `sql.Literal`. Rendering occurs
after target overrides, so plugin SQL uses the actual selected schema.
`PostgresMigrations` opens a connection per plan, acquires the target schema's
advisory lock, and executes that plan transactionally. It does not interpret
business schemas. Only identical targets are deduplicated.

MCP retains `PluginMigrations`. Plugin `migration-plan [--version VERSION]`
uses its storage's `migration(version)` implementation and the same loader.
The host resolves the plan's connection reference from config. It does not
import plugin storage code or manufacture a separate plugin migration engine.
KB and ingest return identical plans for identical shared storage settings.

Common CLI flags: `--migration-version`, `--database`, `--schema`.
The version flag selects the application's directory;
plugins select their latest directory or `[tool.<id>].migration_version`.
Database/schema overrides apply to every target in that invocation. The
selected database must already exist. SQL helpers live in `pg_temp` only.
A new version must be a complete SQL definition, not an incremental fragment.

SQL describes final tables as temporary models, then reconciles only those
names in the target schema. Within those tables it synchronizes columns,
types, defaults, nullability, generated/identity columns, constraints, comments,
indexes and sequences. MCP then reinstalls its grant triggers. Unknown tables
and dependencies are never removed by CASCADE. Impossible conversions, invalid
rows, or foreign dependencies that block an alteration fail the current plan
instead of silently destroying data. Foreign keys/triggers on declared tables
can be recreated on repeated runs; idempotence concerns the resulting schema
and data, not the absence of DDL statements.

Every version is a target definition. Scripts do not assume a particular
source layout, import JSON data, reconstruct users from history, or rewrite
business rows before DDL. Sequence definitions are reconciled without scanning
table values or advancing counters from their maximum IDs.

Tests and testing support were not edited or run in this source phase. No SQL
was applied to `aid` or `aid_ss`. Next verification phase must cover:

- Clean app schemas and existing declared tables with changed/missing/extra
  columns, defaults, types, constraints, indexes, comments and sequences.
- Repeating the same directory without data loss or sequence rollback;
  generated columns, serial-to-identity conversion and KB vector indexes.
- Unrelated tables, including `role_tools` and `schema_migrations`, remain
  byte-for-byte logically unchanged; no previous-state inference from them.
- Incompatible data and incoming foreign dependencies fail transactionally.
- MCP plus enabled plugins, shared KB/ingest deduplication, different schemas
  and credentials, and both DB/schema overrides for unrendered plugin SQL.
- Version selection, missing/empty folders, and absence of data-import inputs
  or prerequisite data-mapping steps.
- SQL package resources in wheels/plugin binaries/Nuitka; adapt testing support
  still using removed `MigrationScripts.packages/package/statement` APIs.
- Full functional/security/build checks, then app-specific migrations against
  `aid` and `aid_ss` and the separate deployment/manual-review phase.

Error boundary: package loading and SQL execution failures are `PostgresError`;
config and malformed plugin plans are `ConfigError`; plugin process
failures retain the launcher's `RuntimeError`. CLI returns nonzero on failure.
Static-check logs for this source phase: `/tmp/boba-sql-migrate-20261104-code/`.
Static analysis is not evidence that migration SQL has passed execution tests.

## 16. Signal and reload contract tests (2026-11-05)

The following suites exercise the signal/reload implementation. They do not
replace migration coverage or a deployment acceptance run.

| Contract | Executable checks |
|---|---|
| `ProcessSignals.on/on_async` | Registration before activation only; KILL/STOP and worker-thread registration rejected; sync actions run in registration order; distinct owners may own distinct signals. |
| `ProcessSignals.__aenter__/__aexit__` | Conflicting owners rejected without partial installation; failed installation restores handlers; replaced external handlers are not overwritten; reuse rejected; queued delivery after exit ignored; callback failures preserve their original causes. |
| `ProcessSignals.cancel_pending` | Async actions are cancelled with their `finally` completed; repeated cancellation is harmless; further async deliveries are disabled; a foreign thread cannot cancel the owner's work. |
| Async signal delivery | A burst during a running action becomes one further pass; the same signal's actions never overlap. |
| `ProcessStop.run/on_stop` | Actions run once for repeated TERM/INT; one failing action does not skip other actions or task cleanup; external task cancellation propagates. |
| `ReloadableMcp.app/reload` | Stable app identity; one startup read; no catalog reads per request; serialized reload; read/validation/startup failures preserve the published cache and routes; retry succeeds. |
| Initial catalog failure | Read, validation or lifespan preparation errors escape initial startup as `McpReloadError`; no snapshot is published and prepared resources close. A failed lifespan requires a new owner. |
| Invalid catalog across process restart | Reserved `/token` and `/token/admin`, unknown tool exports and malformed tool names reject reload without changing persisted rows, old routes or old access grants. Restart against that same database and work directory exits with code 1, reports the cause and leaves both TCP ports closed. Repairing the record permits a fresh start and an actual call on the newly added endpoint. |
| Retrying a rejected SIGHUP | Repeated rejection preserves the current PID and catalog; after repairing the reserved route, SIGHUP publishes all pending changes without restarting. |
| Reload cancellation and shutdown | Cancellation during read/startup closes candidates; shutdown prevents later publication; requests already admitted drain on their old generation; other requests use the new generation. |
| Generation cleanup | Every generation is awaited even if another fails; cleanup errors reach the owner as `McpShutdownError` with an exception group as the cause. |
| `SignalledServer.serve_until_stopped` | Real TERM/INT stop two TCP listeners, active tool calls, keep-alive and incomplete HTTP connections; tool subprocesses exit. Existing server tests also cover repeated stop signals. |
| `McpEndpointStore.list` and SIGHUP | Real PostgreSQL rows/grants become routes and permissions after SIGHUP; bad tool exports or an unavailable table preserve service; repaired catalogs reload successfully. |
| Auth and concurrency across reload | Two users retain separate identities; revoked endpoint grants deny an existing token; deleted routes disappear; both listeners and old/new generations share the running-call limit. |
| `AuthConnection.cancel_safe` | Stopping while catalog SQL waits on an exclusive table lock finishes before that lock is released, including a Kerberos/GSSAPI connection. |

Test files:

- `packages/core/boba-core/tests/test_process_signals.py`
- `packages/core/boba-cancellation/tests/test_stop_contract.py`
- `packages/apps/boba-mcp/tests/test_endpoint_reload.py`
- `packages/apps/boba-mcp/tests/test_reload_network.py`
- Existing regression suites: `test_process_stop.py`, `test_signalled_server.py`
  and the tool-list, calls, limits, endpoints and proxy-login groups in
  `packages/apps/boba-mcp/tests/test_server.py`.

Network tests start `reload_process.py` with two already-bound sockets. They
use the production reload component, endpoint builder, OAuth implementation,
PostgreSQL repository, signal handlers and HTTP server. Tool bodies run in
separate processes. The fixture uses in-memory user rows and role-provider
fixtures; it does not test corporate identity provisioning or sandbox isolation.
The test config selects PostgreSQL authentication; the run on 2026-11-05 used
Kerberos with GSSAPI encryption. Each test creates and removes a unique schema
in the stand's test database, not the deployed application's schema.

Concurrency is synchronized using events and explicit file gates in tool
processes. Deadlines detect hangs; tool completion does not depend on choosing
a sleep longer than reload. Fixture teardown stops the server even on failure.

Use the repository's test environment (`BOBA_RUNTIME`, `BOBA_CONFIG_PATH`,
`BOBA_MCP_CONFIG_PATH` and native-library paths, as configured in
`.vscode/launch.json`). Run through tmux per `.codex/AGENTS.md`:

```sh
.venv/bin/pytest -q \
  packages/core/boba-core/tests/test_process_signals.py \
  packages/core/boba-cancellation/tests/test_stop_contract.py \
  packages/core/boba-cancellation/tests/test_process_stop.py \
  packages/infra/transport/boba-http-server/tests/test_signalled_server.py \
  packages/apps/boba-mcp/tests/test_endpoint_reload.py

.venv/bin/pytest -v -m integration \
  packages/apps/boba-mcp/tests/test_reload_network.py
```
