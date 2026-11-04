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
   the catalog loaded at startup. Connection grants are queried on each use.
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
| `realm text`, `login text` | Normalized identity parts where applicable |

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
`prefix_url = "/dq/all"` gives `/mcp/dq/all`. The same endpoint path appended
to the configured `public_url` gives the public OAuth resource URL.

| Column | Type / default | Meaning |
|---|---|---|
| `id` | `uuid primary key` | Endpoint identity used by grants |
| `prefix_url` | `text unique not null` | Endpoint path relative to the service prefix, e.g. `/all` or `/dq/all` |
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
start with `/`, contain one or more non-empty path segments, have no trailing
slash, query, fragment, scheme, or `.`/`..` segment, and be unique in the MCP
schema. Nested paths are valid. Do not store the service-level `url_prefix`
inside this column. Join paths without a duplicate or missing slash:
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
role grants publishes the configured endpoint. Endpoint changes may take
effect after MCP reload/restart in the first implementation; do not claim
live-reload behavior unless route, OAuth-resource, and MCP tool-list refresh
behavior are implemented together.

Preserve existing prompt behavior by transferring prompt definitions into the
endpoint's `prompts` JSON field. Do not preserve the old endpoint-level role
rules for offering connection sources: endpoint grants and connection grants
replace that policy.

## 7. Migration ownership and rollout

Migration SQL belongs to the application that owns the tables:

- `boba-aid`: AID-owned tables and data only;
- `boba` agent: agent/session/history tables and data only;
- `boba-mcp`: MCP identity, grants, connections, endpoint tables and MCP-owned
  data only.

Shared migration tooling may provide the executor and version tracking, but
each command must select only its application's migration stream. Tool
packages keep migrations for storage they own; one application must not apply
another application's business-table migrations.

The migration system must support both:

1. creating the complete schema on a clean database; and
2. upgrading an existing development schema through ordered, versioned,
   transactional migrations.

There must be no manual `DROP TABLE`, cleanup script, or out-of-band SQL step
for this change. Add upgrade migrations that preserve valid MCP users, roles,
role memberships, connections, and grants; migrate legacy endpoint
configuration and prompt definitions into `mcp_endpoints`; convert each old
TOML endpoint key such as `all` to the relative path `/all` (do not copy
`server.url_prefix` into the row), then remove
obsolete `role_tools` structures from both the AID and MCP schemas. Remove
the `role_tools` DDL from any shared migration package so no clean install
can recreate it. If an old permission cannot be represented in the endpoint
model, report it during migration and resolve it according to the agreed
endpoint grants/tool lists; do not retain `role_tools` as a fallback.

Run each application's migration command against its own schema. The MCP
migration must not create or alter AID or Boba application tables. Keep IDs
for preserved identity and connection rows where the old schema already has
them.

## 8. Implementation order

1. Inventory MCP-owned tables, current endpoint TOML, endpoint OAuth resource
   construction, tool registry assembly, and all `role_tools` references.
2. Define versioned per-application migration streams and clean-install
   baselines without changing runtime behavior yet.
3. Add MCP schema for the endpoint catalog and migrations for upgrading old
   MCP data/configuration. Remove `role_tools` from fresh and upgraded MCP
   schemas.
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
   their own dev schemas and verify data preservation and removal of obsolete
   tables.

Test adaptation is a separate follow-up phase after source implementation, as
requested. That phase must cover migrations, authorization and endpoint
visibility, MCP protocol `tools/list`/`tools/call`, schema overrides,
connection grants, and AID scope forwarding.

## 9. Acceptance criteria

- A clean MCP schema contains MCP-owned users, roles, user_roles, grants,
  connections, and `mcp_endpoints`; it contains no `role_tools` or
  `mcp_endpoint_tools`.
- A clean or upgraded AID schema contains no `role_tools` table or AID
  tool-permission policy.
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
  applicable, and obsolete role-tool data/schema is removed by migration.
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
- `packages/services/boba-runtime/src/boba/runtime/plugins.py` and
  `packages/services/boba-runtime/src/boba/runtime/providers.py`: remove
  plumbing for dynamic role-to-tool permissions from registry assembly where
  it exists solely for MCP; preserve plugin loading and tool construction.
- `packages/services/boba-connection-broker/src/boba/connection_broker/store.py`,
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
  remove shared `role_tools` schema application from AID migration; include
  AID-owned cleanup in the AID migration stream.
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

- `packages/services/boba-runtime/src/boba/runtime/migrate.py`:
  extend the migration runner to apply ordered/versioned upgrades as well as
  clean-install baselines, with transactional failure semantics.
- `packages/apps/boba-aid/src/boba/aid/infra/entry.py`,
  `packages/apps/boba-cli/src/boba/cli/app.py`, and
  `packages/apps/boba-mcp/src/boba/mcp_server/app.py`: give each application
  only its own migration stream. A shared runner is allowed; cross-application
  table migration plans are not.
- Add MCP-owned schema and migration resources under
  `packages/apps/boba-mcp/src/boba/mcp_server/` and include them as package
  data in `packages/apps/boba-mcp/pyproject.toml`.
- `docs/migrations/2026-11-03-database-roles/` and current migration docs:
  update the maintained instructions to explain removal of `role_tools` and
  the versioned app-owned migration streams. Historical applied artifacts can
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
| Per-app migration command | Apply its app's clean-install or upgrade migration stream and data conversion | Serving traffic or applying another application's tables | Migration error aborts that command and transaction, returns nonzero, and identifies the app/version/resource. Never continue with a partially upgraded schema. |

For every new exception, document its owner, what state it guarantees, and
which boundary catches/translates it. Preserve causal chaining. Do not catch
`Exception` merely to return an empty endpoint list or a generic “not
authorized” response.

## 12. Components and structures removed after cutover

Remove these from the active implementation once the database-backed path is
in use:

- PostgreSQL table `role_tools`, its SQL resource
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
| Connection parameter publication | `packages/services/boba-connection-broker/src/boba/connection_broker/params.py`: `published()`; no source-offer filtering |
| Shared migration execution | `packages/infra/db/boba-db-postgres/src/boba/db/postgres/migrate.py`: versioned `MigrationPlan`, `MigrationScripts.application()`, `PostgresMigrations` |
| Migration composition | `packages/services/boba-runtime/src/boba/runtime/migrate.py`: `MigrationPreparation`, `ApplicationMigrations` |
| Legacy MCP endpoint data | `packages/apps/boba-mcp/src/boba/mcp_server/migration.py`: `McpMigrationInput`; prepares input for SQL, performs no database writes itself |

Application SQL is in `schema/`, `adoption/`, `upgrades/` under `boba.aid`,
`boba.cli`, `boba.mcp_server`. MCP upgrades import endpoint data, remove
`role_tools` / `mcp_endpoint_tools`, and enforce grant references plus
transactional grant deletion. The migration ledger is `schema_migrations`;
checksums protect applied versions. Existing SQL resources in shared packages
are no longer selected by application commands; old test fixtures still
reference some of them and must move to app-owned streams in the next phase.

The implementation removes `RoleToolsTable`, its DDL, `RolePermissions`,
`role_permissions`, `RoleTools` and `RoleToolServers`, `ConnectionOfferRule`,
`OfferedSource`, `ConnectionParams.offered()`, and MCP endpoint TOML config.
`McpToolProvider` now returns exactly the application's tool set; it has no
parallel unconditional-operations list. AID relay was inspected: it already
has no role/tool ACL, so its audience/scope adaptation was retained.

### Operational details for the next agent

- `prefix_url` is a relative service route starting with `/`, such as
  `/dq/all`. `[server].url_prefix` remains the service prefix. OAuth scopes
  now use endpoint paths, so old OAuth sessions need reauthentication.
- Endpoint catalog and grants are loaded at startup. Updating them requires
  an MCP restart. User-role changes take effect at sign-in/refresh; there are
  no DB reads during JWT verification. Connection grants remain per-call DB
  checks. Do not reintroduce users/roles queries into token verification.
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
- `boba-mcp migrate --config ... --import-endpoints <json>` imports resolved
  legacy TOML endpoint sections once. Wildcards are expanded during import,
  not retained in the database. Conflicting legacy role-to-tool grants abort
  the migration with endpoint/role/tool context.
- The current local compose endpoint definitions and prompts were exported
  to `compose/mcp/conf/migrations/endpoints.json` before removing those TOML
  sections. This local deployment-data file is ignored by Git. Compose mounts
  its directory for the migration command. The `.vscode/boba/mcp.toml` overlay
  inherits the process-only config and selects database `aid_ss`; it has no
  endpoint policy to remove. Transfer the import artifact when deploying to
  a different machine. Do not recreate defaults on each service startup.
- Package-data declarations include app SQL directories. The existing Make /
  `boba.core.package_data` inventory consumes them; no separate SQL-copy build
  step was added. Binary builds and package-resource checks are pending.

### Separate verification and rollout phase

The next agent owns test changes, test execution, and correction of findings.
At minimum, adapt the previously listed test locations and cover:

1. Fresh app schemas, adoption of the existing schemas, repeated migrate,
   checksum mismatch, migration rollback, wrong-app schemas, and preservation
   of user/role/connection IDs and grants.
2. Removal of obsolete tables, transfer of legacy endpoint/prompt data,
   conflicting legacy permissions, grant integrity and resource deletion.
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

## 15. Migration ownership follow-up (2026-11-04)

The MCP `migrate` command **must also migrate enabled plugins**. It coordinates
their declared storage plans through the same executor as its own plan. Do not
remove `PluginMigrations` from MCP or copy plugin SQL into the MCP package.
AID and Boba keep their separate `migrate` entry points and application SQL.
The common `ApplicationMigrations` runner is shared infrastructure, not a
combined application schema.

Each storage owner now ships `migrations.toml` next to its SQL. `tables` lists
its final tables; `legacy_tables` lists the tables its upgrades retire. The
declarations are located in `boba.aid`, `boba.cli`, `boba.mcp_server`,
`boba.db.pgvector`, and `boba.tool.describer`. They are package resources,
not additional deployment configuration. Package-data declarations include
the manifest and adoption SQL.

`MigrationScripts.application()` reads the owner's manifest and SQL into a
`MigrationPlan.definition`. The same method is used by applications and plugin
storage implementations. KB substitutes the configured table names and uses
a version stream identifying that table set. KB and ingest share the same
storage implementation and therefore provide identical plans when configured
for the same storage. Conflicting plans abort before any migration SQL.

The plugin process still exports its plan through `migration-plan`; MCP's
`PluginMigrations` resolves only the connection reference and passes the plan
to `PostgresMigrations`. It no longer manufactures version stream names or a
no-op adoption script. Plugins must provide a versioned plan with ownership.
`boba.db.pgvector/adoption/0001_legacy.sql` and
`boba.tool.describer/adoption/0001_legacy.sql` validate their existing tables.
Rebuild plugin binaries along with MCP before validating sandbox deployment.

The executor checks declared table ownership against other supplied plans
and the persisted baseline rows in `schema_migrations.tables`. With no version
history, only existing tables belonging to that component trigger adoption;
other components' tables may coexist in the schema. After SQL, the executor
checks that all declared final tables exist, declared legacy tables are gone,
and no undeclared table was created or removed in the target schema. Failure
rolls back the current transaction. This is an ownership check for package SQL,
not an isolation boundary for executing untrusted SQL.

Execution preserves first-seen order: MCP's own stream precedes plugins.
Plans share a transaction only when both schema and connection configuration
match (pool sizing and application label do not change connection identity).
Different credentials are not silently replaced by the first component's
credentials. Completed transactions for other connections/schemas cannot be
rolled back if a later component fails; rerunning the command skips completed
versions. No claim of a cross-database transaction is made.

This is a source-code phase. Tests and testing support were not edited or run,
and no SQL was applied to `aid` or `aid_ss`. Before applying those databases,
the next verification phase must additionally cover:

- MCP plus plugin tables in one fresh schema; a newly enabled plugin in an
  already versioned schema; separate schemas and separate DB credentials.
- Shared KB/ingest plan deduplication, conflicting table owners and plans,
  adoption of old KB/describer data, and repeat migrations.
- A plan creating or removing an undeclared table, a missing final table,
  a remaining retired table, and transactional rollback of each failure.
- The new manifests and SQL in wheels, plugin images and Nuitka inventories.
- The separate app commands against both `aid` and `aid_ss`, with the MCP
  endpoint import artifact and enabled plugin plans, after those checks pass.

Static-check logs for this phase are under `/tmp/boba-migrations-20261104-code/`.
They are not evidence of a successful functional migration test.

Completed static checks: Pyright on the changed Python modules (zero errors),
Ruff over `packages`, `sec/sec.sh sast` (exit 0; Semgrep zero findings), and
`git diff --check`. `make -C build version` reports `0.0.33.dev1`. The first
SAST run found an existing overlong module docstring in MCP; its line wrapping
was corrected before the successful rerun. Full builds remain in the next
verification phase.

Error boundaries: package-definition/SQL resource errors and executor failures
are `PostgresError`; malformed plugin wire plans and missing ownership are
`ConfigError`; plugin process failures retain the launcher's `RuntimeError`
with plugin/command context. Application CLI entry points turn these failures
into a nonzero exit. No fallback publishes a successful migration result.
