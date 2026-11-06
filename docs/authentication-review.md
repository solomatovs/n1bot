# Authentication architecture review

Reviewed 2026-11-04. This document describes the current authentication code and the refactoring direction. It does not claim that the full test suite is green; the test adaptation and final regression run remain a separate stage.

The 2026-11-04 production changes were checked with Ruff and Pyright on the changed source files. Pytest was not run; existing test imports and fixtures will be adapted in the separate test stage.

## Summary

Authentication is already partly shared. AID, boba-mcp and `boba serve` build providers through `SignInAssembly`, use the same identity and role models, and pass successful sign-ins to a common `AuthServer` or `AuthService`. User identity is represented as a canonical login, independent of whether the identity came from local credentials, LDAP, Kerberos or a trusted proxy.

The first dependency boundary has now been corrected: SPNEGO acceptance and ticket handling live in the runtime infrastructure layer, connection credential issuance lives in the connection broker, and Kerberos sign-in policy remains in `boba-auth` behind verified facts. `boba-auth` no longer needs the concrete Kerberos runtime or connection packages. Session and resource access-token checks are now stateless: they validate signed claims locally and do not query users or roles. Database reads remain at sign-in and explicit token renewal, where current role assignments are used to issue the next token.

External JWT/OIDC bearer sign-in is deferred. Existing Boba OAuth bearer tokens authenticate calls to MCP and the agent service; they are service access tokens, not a configured external identity-provider method. MCP keeps OAuth as its only service-token protocol.

## Current component map

| Component | Responsibility | Depends on / used by |
| --- | --- | --- |
| `boba-identity` | Identity, session, sign-in metadata, error and persistence ports; role and access models | Shared by auth, AID, MCP, OAuth and runtime |
| `boba-auth` | Local and LDAP password sign-in, Kerberos sign-in policy, proxy HMAC sign-in, shared result/role/profile mapping, app JWT and session lifecycle | `SignInAssembly`, AID callbacks and OAuth server |
| `boba-runtime.SignInAssembly` | Reads the configured provider list and composes providers with directory, profile and database role ports | AID, MCP and `boba serve` |
| `boba-runtime.signin_kerberos` | Concrete SPNEGO acceptor, delegated ticket capture and sealing adapter | Runtime provider composition |
| `boba-identity-postgres` | PostgreSQL users, identities and role membership adapter | AID, MCP, agent service |
| `boba-access-postgres` | PostgreSQL access grants and role/tool permissions | MCP and tool execution |
| `boba-krb` | SPNEGO accept, delegated ticket capture/sealing and Kerberos ticket operations | Runtime Kerberos adapter and connection broker credential source |
| `boba-oauth` | OAuth 2.1 login, authorization codes, service access/refresh tokens and MCP resource-token checks | MCP and `boba serve` |
| `boba-aid.auth` | Chainlit HTTP, socket, cookie, password, SSO and proxy adapters | AID only |
| `boba-mcp` / `boba serve` | OAuth resource server and service composition | Shared OAuth server and sign-in assembly |

`boba-identity` is a broad shared package rather than a minimal authentication model package: alongside identity it contains call context, cancellation-related run state and locks. Its contracts are infrastructure-independent, but a later package review should check whether call execution contracts belong there or in a separate shared runtime model package. This should be a deliberate package boundary change, not a file-by-file split.

## Authentication methods by service

| Configured method | AID | boba-mcp | `boba serve` | Current state |
| --- | --- | --- | --- | --- |
| `local` | Chainlit password form | OAuth login form | OAuth login form | Shared password provider |
| `ldap` | Chainlit password form | OAuth login form | OAuth login form | Shared LDAP provider and role mapping |
| `kerberos` | SPNEGO login and refresh routes | SPNEGO on OAuth login | SPNEGO on OAuth login | Common sign-in logic, but coupled to concrete Kerberos infrastructure |
| `proxy` | Signed HTTP endpoint | OAuth JWT-bearer assertion exchange | OAuth JWT-bearer assertion exchange | Shared HMAC identity provider; transport adapters differ |
| Boba OAuth bearer | Not an identity-provider login method | Protects MCP resource calls | Protects agent service calls | Internal service access token |
| External JWT/OIDC bearer | Deferred | Deferred; OAuth remains | Deferred | Deliberately out of the current refactoring scope |

The `[app].auth` list is a discriminated list of enabled methods. Empty means no user sign-in methods are configured. Configuration permits one Kerberos backend and one proxy backend. Local and LDAP password backends are tried in configuration order. The available methods are exposed through common `SignIns`, but each transport chooses which method can be invoked at a particular route. There is no generic transport-neutral “authenticate this credential” endpoint yet.

## Identity and authorization flow

1. A configured provider verifies its credential and returns `SignedIn` with a `UserIdentity`.
2. The identity rule maps the provider login into a canonical login. Applications compare this identifier rather than treating the provider name as the user identity.
3. Provider-specific role rules produce candidate role names. `RoleCatalog` resolves those against roles that exist in the database. Automatic assignment therefore cannot create a role absent from the DB.
4. `require_roles` decides whether an authenticated user with no resolved roles may enter. AID, MCP and `boba serve` use the same admission logic.
5. At sign-in, the service persists the user and role membership. AID session JWTs and MCP/agent OAuth tokens carry the user UUID, identity and granted roles/scopes. Token verification checks these signed claims locally without a user-table lookup.

The database remains authoritative when a user signs in or explicitly renews a token. The AID JWT carries the persisted user UUID and creation time so Chainlit can rebuild its user without a lookup. Roles changed or removed after issuance continue to be present in an existing access token until it expires; renewal reads current membership and can narrow the next token. Deleting a user likewise does not immediately invalidate existing access tokens. Global invalidation remains available by changing the session generation. This is the trade-off for not consulting the database on each token check.

The verified JWT is the source of per-sign-in identity, roles, profile selection
and delegated credentials during a chat session. `ChainlitSession.signed_in()`
parses that token through the shared `SessionClaims` model and checks that a
hydrated Chainlit user, when present, has the same login and UUID. It does not
reconstruct authentication state from `session.user.metadata`, which is a
persistence projection and may omit the provider or Kerberos ticket. This keeps
the transport adapter responsible for binding the session while the shared
identity model remains the authority for its claims.

AID file and journal HTTP routes use the same rule. Their request dependency
extracts the session token from the configured cookie (or a bearer header),
calls `AuthService.user_of_token()`, and passes an `AuthenticatedUser` to the
route. The route no longer reads roles from Chainlit `PersistedUser.metadata`;
the Chainlit object remains a compatibility projection for Chainlit itself.

## Kerberos ticket in the JWT

The current code does carry the delegated ticket in the issued sign-in JWT:

1. `SpnegoGate` accepts the browser's SPNEGO token.
2. `SsoSignIn` captures delegated credentials and `TicketSealer` encrypts/authenticates the serialized ticket with Fernet. The key is derived from the shared session secret.
3. `SignInMetadata.sealed_ticket` renders as `metadata.sso_ticket` in the JWT payload.
4. The JWT itself is signed with HS256; it is not encrypted. The ticket value inside it is ciphertext, so decoding the JWT payload does not reveal the Kerberos ticket.
5. The connection credential adapter opens that ciphertext only when a delegated Kerberos connection needs credentials.

Existing checks cover issuance, persistence separation, session use and OAuth
refresh: `test_sign_in_puts_principal_and_ticket_into_the_jwt`,
`test_users_row_keeps_identity_while_ticket_stays_in_jwt`,
`test_signed_in_session_carries_the_sealed_ticket`, and
`test_sso_ticket_survives_oauth_refresh_and_mcp_restart`. The main test set ran
on 2026-11-04 with 3368 passed, 2 expected failures and no failures. The
`integration` marker suite is being run separately because pytest excludes it
by default.

## Dependency and boundary findings

### Concrete infrastructure in the shared auth package

Before this pass, `boba-auth` imported `SpnegoAcceptor`, `TicketCapture`, `TicketSealer` and `SsoTickets` from `boba-krb`; its `credentials` module also imported connection classes and a concrete service-ticket issuer. SPNEGO verification and ticket sealing now live in `boba-runtime.signin_kerberos`; user mapping, role admission and sign-in metadata remain in `boba-auth.sso`, which accepts verified `PrincipalFacts`. Connection credential issuance now lives in `boba-connection-broker.credentials`. The reusable auth service no longer imports concrete `boba-krb` or `boba-connections` modules. The next step is to define the remaining provider contract cleanly and ensure future authentication implementations follow the same boundary.

### Two JWT/JWS implementations

The dependency graph includes PyJWT for AID sign-in tokens and `joserfc` through the OAuth implementation. There are distinct protocols around the two tokens, but both packages provide JOSE/JWT functionality. Before adding another JWT dependency for OIDC, evaluate using one JOSE implementation for internal sign-in tokens and external-token validation. Keep the FastMCP-required implementation where its integration requires it; do not replace it solely for package-count optics.

### Infrastructure and transport dependencies

The OAuth server depends on FastMCP/MCP and Starlette, which is appropriate for the transport component. AID's Chainlit/FastAPI code is isolated under `boba-aid.auth`. The next boundary should keep these adapters out of `boba-auth`; a new service should be able to compose identity providers without importing Chainlit or FastMCP.

### External bearer trust requirements

This is future guidance only; external JWT/OIDC bearer is deferred. Any later implementation must validate issuer, audience, expiry and not-before claims, pin accepted signing algorithms, select keys by `kid`, refresh JWKS safely, and reject tokens from unconfigured issuers. The provider must explicitly map a verified stable subject to a canonical login. Role claims must be treated as candidates and intersected with the DB role catalog.

### Security findings and decisions

- `[auth.local.users]` is a plaintext login/password table. The current AID and MCP compose TOML files contain literal local passwords. Local sign-in compares the submitted password directly with this value; it does not support password hashes.
- Proxy HMAC requests are accepted inside a timestamp window, but there is no replay store. OAuth proxy assertions carry `jti`, yet the verifier does not remember used IDs. A captured signed request can be replayed while its timestamp or assertion remains valid. The user chose to avoid DB-backed token/replay storage and DB reads during token verification; this replay window remains an accepted design limitation for now.
- AID proxy configuration currently accepts `0.0.0.0/0` in compose. That disables the network-source restriction; HMAC still gates the request, but the CIDR is not an additional control in that configuration.

These are separate policy and storage decisions, not hidden as implementation details in the provider refactor.

## Target component boundaries

| Layer | Owns |
| --- | --- |
| Shared identity contracts | `UserIdentity`, `SignedIn`, sign-in metadata, provider ports, role/user persistence protocols and auth errors; no HTTP, DB or Kerberos client |
| Authentication service | Provider composition, common sign-in result, admission and role resolution, session/token policy, identity mapping; depends on ports and small crypto/token primitives |
| Infrastructure providers | PostgreSQL persistence, LDAP directory client, Kerberos/SPNEGO and ticket sealing, OIDC discovery/JWKS verification |
| Service composition | Reads TOML/OmegaConf, constructs only configured providers and injects infrastructure adapters |
| Transport adapters | AID/Chainlit and OAuth/FastMCP endpoints; expose only configured provider flows and translate transport errors |

Prefer composition through provider interfaces. Use inheritance only where a framework requires it. Keep provider implementations at a useful size: one package per real infrastructure boundary, not one package per class.

## Refactoring sequence

1. **Authentication contract and audit:** document current flows and dependency edges; preserve the rule that only configured methods are enabled.
2. **External bearer decisions:** choose whether external JWTs are exchanged for internal sessions or checked directly, and choose claim mapping defaults. Then define one provider config for issuer, audience, login mapping and allowed claims.
3. **Shared provider contract:** add a transport-neutral bearer sign-in port and a common result/admission path. All role candidates continue through the DB role catalog.
4. **Kerberos boundary:** keep SPNEGO acceptance and ticket sealing in the runtime infrastructure layer, and keep connection credential sourcing with the connection broker. Preserve the current encrypted-ticket-in-signed-JWT contract.
5. **Service wiring:** have AID, MCP and `boba serve` build the same provider set from `[app].auth`; reject unknown or duplicate backends and ensure each transport exposes exactly the configured methods.
6. **Dependency convergence:** after import boundaries are clean, remove redundant direct dependencies and compare resolved dependency trees for AID, MCP and `boba serve`.
7. **Separate test/adaptation stage:** once production code is stable, adapt existing tests as a separate task, run focused provider tests, then AID/MCP integration and the full project suite. Test failures must be classified as product defects or stale test assumptions before changing behavior.

## Pending decisions

- External JWT/OIDC bearer remains a future, separate feature; no claim contract or token exchange is being implemented now.
- Decide how to replace plaintext local development passwords in compose TOML. No configuration change has been made; the values may be disposable dev credentials.
