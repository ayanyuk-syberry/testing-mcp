# testing-mcp

Experimental FastAPI service that doubles as an MCP server, built with
[fastapi-mcp](https://github.com/tadata-org/fastapi_mcp): regular REST endpoints are
automatically exposed as MCP tools over streamable HTTP — now protected with **OAuth 2.1**
per the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
with [Keycloak](https://www.keycloak.org/securing-apps/mcp-authz-server) as the authorization server.

## Architecture

Client identity uses **CIMD** (Client ID Metadata Documents, SEP-991 — the preferred
mechanism since the 2025-11-25 MCP spec): the client's `client_id` *is a URL* hosted by
its vendor; Keycloak fetches the metadata from that URL on demand. No dynamic client
registration, no client records accumulating per auth attempt, no registration proxy.

```
┌─────────────┐  1. POST /mcp (no token) ──► 401 + WWW-Authenticate   ┌──────────────────┐
│ Claude Code │  2. GET /.well-known/oauth-protected-resource         │ FastAPI app      │
│ (OAuth      │ ────────────────────────────────────────────────────► │ :8000            │
│  client)    │     "authorization_servers": [keycloak realm]         │ (resource server)│
│             │                                                       └──────────────────┘
│             │  3. authorize with client_id = <metadata URL>  ┌──────────────────┐
│             │ ─────────────────────────────────────────────► │ Keycloak :8080   │
│             │     Keycloak fetches the client metadata URL,  │ realm "mcp"      │
│             │     validates it against the CIMD policy       │ (authorization   │
│             │  4. browser → user logs in (alex/alex123)      │  server,         │
│             │     and consents to the client                 │  --features=cimd)│
│             │  5. token request (PKCE)                       │                  │
│             │ ◄─────────────── access token (JWT) ────────── │                  │
│             │                                                └──────────────────┘
│             │  6. POST /mcp  Authorization: Bearer <JWT>  ──► validated against
└─────────────┘                                                  Keycloak JWKS + audience
```

The FastAPI app never sees a password — it only **validates JWTs** (signature via
Keycloak's JWKS, issuer, and audience `http://localhost:8000/mcp`) in
`app/auth.py:verify_token`. The `whoami` tool reads the user's identity from token claims.

## Run

```bash
# 1. Authorization server (Keycloak with pre-imported realm "mcp")
docker compose up -d        # takes ~30s; admin console at :8080 (admin/admin)

# 2. The API / MCP server
uv run uvicorn app.main:app --reload --port 8000
```

- REST: http://localhost:8000/hello (public), http://localhost:8000/me (needs token), docs at /docs
- MCP (streamable HTTP, OAuth-protected): http://localhost:8000/mcp
- Test user: **alex / alex123**

> Note: this machine's shell exports `UV_DEFAULT_INDEX` pointing at a private
> CodeArtifact mirror. If its token is expired, prefix uv commands with
> `UV_DEFAULT_INDEX=https://pypi.org/simple`.

## Connect from Claude Code

```bash
claude mcp add --transport http hello-mcp http://localhost:8000/mcp
```

Then inside a Claude Code session run `/mcp` → select `hello-mcp` → **Authenticate**.
A browser opens on the Keycloak login page; sign in as `alex` / `alex123`. After that the
`hello_world` and `whoami` tools work, and `whoami` returns the logged-in user.

## What implements what

| Piece | Where |
|---|---|
| Token validation (JWKS, issuer, audience) | `app/auth.py` |
| 401 + `WWW-Authenticate` pointing at resource metadata (RFC 9728 §5.1) | `app/auth.py:_unauthorized` |
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
- **`test-cli` client** allows headless testing via the password grant:

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/realms/mcp/protocol/openid-connect/token \
  -d 'grant_type=password&client_id=test-cli&username=alex&password=alex123&scope=openid' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
curl -s http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
```

## Security rules baked in (from the MCP auth spec)

- Token must arrive as `Authorization: Bearer ...` on **every** request — never in the URL.
- The server validates the token was issued **for this server** (audience check) — it must
  not accept arbitrary tokens from the same IdP, and must never forward the client's token
  upstream (token passthrough is forbidden).
- Invalid/missing token → `401` with a `WWW-Authenticate` header pointing at the resource
  metadata; insufficient permissions → `403`.

## Toward real hosting

- Serve everything over HTTPS (OAuth endpoints require it for non-localhost), and turn
  off `cimd-allow-http-scheme`.
- Replace the sandbox Keycloak (dev mode, file DB, permissive policies) with a hardened
  instance or a hosted IdP with MCP support (Auth0, WorkOS, etc.).
- Tighten the CIMD trusted-domain list to exactly the clients you expect, and remove the
  legacy anonymous-DCR registration policy.
