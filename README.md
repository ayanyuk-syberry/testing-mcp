# testing-mcp

Experimental FastAPI service that doubles as an MCP server, built with
[fastapi-mcp](https://github.com/tadata-org/fastapi_mcp): regular REST endpoints are
automatically exposed as MCP tools over streamable HTTP — now protected with **OAuth 2.1**
per the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
with [Keycloak](https://www.keycloak.org/securing-apps/mcp-authz-server) as the authorization server
and [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) as the token-verifying
gatekeeper in front of the app.

## Architecture

Client identity uses **CIMD** (Client ID Metadata Documents, SEP-991 — the preferred
mechanism since the 2025-11-25 MCP spec): the client's `client_id` *is a URL* hosted by
its vendor; Keycloak fetches the metadata from that URL on demand. No dynamic client
registration, no client records accumulating per auth attempt, no registration proxy.

The public port :8000 is owned by **oauth2-proxy** (docker), which validates every
bearer JWT (signature via Keycloak's JWKS, issuer, audience `http://localhost:8000/mcp`
— `--skip-jwt-bearer-tokens`) and proxies to the app on :8001, which **trusts the proxy**
and only decodes the forwarded token for its claims:

```
┌─────────────┐  1. POST /mcp (no token) ──► bare 401            ┌───────────────────┐
│ Claude Code │  2. GET /.well-known/oauth-protected-resource    │ oauth2-proxy      │
│ (OAuth      │ ───────────────────────────────────────────────► │ :8000 (docker)    │
│  client)    │     passed through unauthenticated to the app;   │ verifies JWTs,    │
│             │     "authorization_servers": [keycloak realm]    │ 401s API routes   │
│             │                                                  └────────┬──────────┘
│             │  3. authorize with client_id = <metadata URL>            │ forwards
│             │ ──────────────────────────────────► ┌──────────────────┐ │ verified JWT
│             │     Keycloak fetches the client     │ Keycloak :8080   │ ▼
│             │     metadata URL, validates it      │ realm "mcp"      │ ┌───────────────┐
│             │     against the CIMD policy         │ (authorization   │ │ FastAPI app   │
│             │  4. browser → user logs in          │  server,         │ │ :8001 (host)  │
│             │     (alex/alex123) and consents     │  --features=cimd)│ │ decodes claims│
│             │  5. token request (PKCE)            │        ▲         │ │ trusts proxy  │
│             │ ◄──── access token (JWT) ────────── │        │ JWKS    │ └───────────────┘
│             │                                     └────────┼─────────┘
│             │  6. POST /mcp  Bearer <JWT> ──► oauth2-proxy ─┘ (in-network keycloak:8080)
└─────────────┘
```

The app never sees a password and no longer verifies signatures itself — verification
lives in oauth2-proxy; `app/auth.py:verify_token` just decodes the proxy-forwarded JWT
(`decode_forwarded_claims`). The `whoami` tool reads the user's identity from token claims.

> **Trust boundary caveat:** anything that reaches uvicorn directly on :8001 bypasses
> authentication entirely. Fine for this localhost sandbox; real hosting must make the
> app port reachable only by the proxy.

Note the 401 nuance: oauth2-proxy's 401 carries no `WWW-Authenticate: resource_metadata`
header (the app's pre-proxy 401 did). Claude Code's discovery chain doesn't need it — it
proactively checks `/.well-known/oauth-protected-resource` (then falls back to RFC 8414
`/.well-known/oauth-authorization-server`), and those paths pass through the proxy
unauthenticated (`--skip-auth-routes`). If a client ever fails here, pin discovery with
`"oauth": {"authServerMetadataUrl": "http://localhost:8080/realms/mcp/.well-known/openid-configuration"}`
in its MCP server config.

## Run

```bash
# 1. Keycloak (authorization server) + oauth2-proxy (gatekeeper on :8000)
docker compose up -d        # takes ~30s; admin console at :8080 (admin/admin)

# 2. The API / MCP server — on :8001, BEHIND the proxy
uv run uvicorn app.main:app --reload --port 8001
```

- REST (all through the proxy): http://localhost:8000/hello (public),
  http://localhost:8000/me (needs token), docs at /docs
- MCP (streamable HTTP, OAuth-protected): http://localhost:8000/mcp
- Browser cookie flow (manual test of the proxy itself): http://localhost:8000/oauth2/sign_in
- Test user: **alex / alex123**

> Note: this machine's shell exports `UV_DEFAULT_INDEX` pointing at a private
> CodeArtifact mirror. If its token is expired, prefix uv commands with
> `UV_DEFAULT_INDEX=https://pypi.org/simple`.

## Connect from Claude Code

```bash
claude mcp add --transport http hello-mcp http://localhost:8000/mcp
```

Then inside a Claude Code session run `/mcp` → select `hello-mcp` → **Authenticate**.
A browser opens on the Keycloak login page; sign in as `alex` / `alex123`. After that all
the tools work: `hello_world`, `whoami` (returns the logged-in user), and the job-pattern
tools `start_job` / `get_job_status` / `get_job_result` / `cancel_job` (implemented in
`app/jobs.py`, for long-running tool calls).

### Alternative: static pre-registered client (instead of CIMD)

The realm also carries a `claude-code` client demonstrating the third registration model —
manual pre-registration (what CIMD and DCR exist to avoid). Connect with an explicit
`client_id` and the callback port matching its registered redirect URI:

```bash
claude mcp add --transport http hello-mcp-static http://localhost:8000/mcp \
  --client-id claude-code --callback-port 8765
```

## Alternative implementation: FastMCP (`fastmcp_app/`)

`fastmcp_app/` is a second implementation of the *same* `hello-mcp` server, built on the
**MCP SDK's `FastMCP`** (`mcp.server.fastmcp`) instead of fastapi-mcp. Same Keycloak
realm, same `http://localhost:8000/mcp` audience, same tools (`hello_world`, `whoami`, and
the job pattern) — it's a side-by-side comparison of the two ways to stand up an
OAuth-protected MCP server:

| | `app/` (fastapi-mcp) | `fastmcp_app/` (FastMCP) |
|---|---|---|
| Tools defined by | REST endpoints + `operation_id` | native `@mcp.tool()` decorators |
| Token validation | FastAPI `Depends(verify_token)` (`app/auth.py`) | `TokenVerifier.verify_token` (`fastmcp_app/auth.py`) |
| RFC 9728 metadata route | hand-written in `app/main.py` | auto-served by setting `AuthSettings.resource_server_url` |
| Reading caller claims in a tool | `claims` injected via the dependency | `get_access_token().claims` |
| Tool errors | FastAPI `HTTPException` | `ToolError` |

Run it **instead of** the FastAPI app (it takes the same port 8001 behind the proxy, so
the two don't run at once):

```bash
uv run uvicorn fastmcp_app.main:app --reload --port 8001
claude mcp add --transport http hello-mcp-fastmcp http://localhost:8000/mcp
```

> One difference from the fastapi-mcp version: FastMCP auto-serves only the path-suffixed
> metadata route (`/.well-known/oauth-protected-resource/mcp`), not the bare-root variant
> `app/main.py` also serves manually. Claude Code uses the suffixed form, so this is fine.

## What implements what

| Piece | Where |
|---|---|
| Token verification (JWKS, issuer, audience) | oauth2-proxy (`docker-compose.yml`: `SKIP_JWT_BEARER_TOKENS` + `OIDC_EXTRA_AUDIENCES`) |
| Decoding the proxy-forwarded token (no re-verification) | `app/auth.py:decode_forwarded_claims` (shared by both flavours) |
| Job pattern for long-running tools (`start_job` / `get_job_status` / `get_job_result` / `cancel_job`) | `app/jobs.py` (mounted in `app/main.py`; reused by `fastmcp_app/main.py`) |
| 401 + `WWW-Authenticate` pointing at resource metadata (RFC 9728 §5.1) | `app/auth.py:_unauthorized` (mostly moot behind the proxy — its 401s are bare) |
| Protected resource metadata (RFC 9728) | `app/main.py:protected_resource_metadata` |
| AS metadata, login UI, consent, token issuance, PKCE | Keycloak (`keycloak/realm-mcp.json`) |
| Client identity (CIMD / SEP-991) | Keycloak `--features=cimd` + `clientProfiles`/`clientPolicies` in the realm file |
| Token audience binding (RFC 8707 substitute) | Keycloak audience mapper in realm-default scope `hello-mcp-claims` (duplicated in `profile` for clients without realm defaults) |

## Keycloak realm notes (`keycloak/realm-mcp.json`)

- **Realm `mcp`** is imported automatically on container start (`--import-realm`).
- **CIMD client policy** (`clientProfiles`/`clientPolicies` in the realm file): URL
  client_ids are accepted when their domain matches the trusted list (`claude.ai`,
  `claude.com`, `anthropic.com` + subdomains, plus `host.docker.internal`/`localhost`
  for local testing). `cimd-allow-http-scheme` is on for the sandbox; production would
  be https-only with a tight domain list. CIMD is experimental in Keycloak 26.6
  (`--features=cimd`). If a client's domain isn't trusted, the authorize request fails
  with `client_not_found` — check `docker logs` for `not trusted domain: host = ...`
  and extend the list.
- **CIMD clients require user consent** (Keycloak forces it for URL-identified
  clients) — the browser flow shows a grant screen after login.
- **`offline_access` needs a realm role**: MCP clients request the `offline_access`
  scope for refresh tokens, and Keycloak only issues offline tokens to users holding the
  `offline_access` realm role — granted to the test user in the realm file.
- **Why claims live in client scopes**: the audience + identity mappers sit in the
  realm-default scope `hello-mcp-claims` (and duplicated in `profile`), so any client —
  including transient CIMD clients — mints tokens our resource server accepts.
- **Anonymous DCR remains available** (Trusted Hosts policy, localhost redirect URIs
  only) as the legacy fallback, but spec-compliant clients prefer CIMD when the AS
  advertises `client_id_metadata_document_supported: true`. Note Keycloak's DCR has a
  quirk: it sets a new client's scopes to exactly the registration request (wiping realm
  defaults), which then trips its strict authorize-time scope validation — that's why
  the DCR era of this repo needed a registration proxy (see git history).
- **Static `claude-code` client** is pre-registered in the realm as the third
  registration model — manual pre-registration, the classic approach CIMD and DCR exist
  to avoid. It has a fixed `client_id` (`claude-code`) and a fixed localhost redirect URI,
  so clients must supply both a matching `--client-id` and `--callback-port` (see
  "Alternative: static pre-registered client" above).
- **`oauth2-proxy` client** is the realm's only confidential client (has a `secret`); the
  proxy uses it for the browser cookie flow at `/oauth2/sign_in`. The bearer-JWT path MCP
  clients take needs only the realm's public JWKS. Related: `KC_HOSTNAME` is pinned to
  `http://localhost:8080` in docker-compose so tokens redeemed in-network (at
  `http://keycloak:8080`) still carry the `localhost` issuer the proxy expects.
- **`test-cli` client** allows headless testing via the password grant:

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/realms/mcp/protocol/openid-connect/token \
  -d 'grant_type=password&client_id=test-cli&username=alex&password=alex123&scope=openid' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
curl -s http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
```

## Security rules baked in (from the MCP auth spec)

- Token must arrive as `Authorization: Bearer ...` on **every** request — never in the URL.
- The resource server side validates the token was issued **for this server** (audience
  check, now enforced by oauth2-proxy's `--oidc-extra-audiences`) — it must not accept
  arbitrary tokens from the same IdP, and must never forward the client's token to
  *third-party* upstreams (proxy → its own resource server, as here, is the one legitimate
  hop; token passthrough beyond the resource server is forbidden).
- Invalid/missing token → `401` (from the proxy; bare, no `resource_metadata` hint —
  clients rely on well-known discovery); insufficient permissions → `403`.

## Toward real hosting

- Serve everything over HTTPS (OAuth endpoints require it for non-localhost), and turn
  off `cimd-allow-http-scheme`.
- Replace the sandbox Keycloak (dev mode, file DB, permissive policies) with a hardened
  instance or a hosted IdP with MCP support (Auth0, WorkOS, etc.).
- Tighten the CIMD trusted-domain list to exactly the clients you expect, and remove the
  legacy anonymous-DCR registration policy.
- Close the trust-boundary hole: make the app on :8001 unreachable except from
  oauth2-proxy (private network / container network / mTLS), or re-enable in-app JWT
  verification as defense in depth.
