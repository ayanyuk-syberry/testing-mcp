# testing-mcp

Experimental FastAPI service that doubles as an MCP server, built with
[fastapi-mcp](https://github.com/tadata-org/fastapi_mcp): regular REST endpoints are
automatically exposed as MCP tools over streamable HTTP — now protected with **OAuth 2.1**
per the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization),
with [Keycloak](https://www.keycloak.org/securing-apps/mcp-authz-server) as the authorization server.

## Architecture

```
┌─────────────┐  1. POST /mcp (no token) ──► 401 + WWW-Authenticate   ┌──────────────────┐
│ Claude Code │  2. GET /.well-known/oauth-protected-resource         │ FastAPI app      │
│ (OAuth      │ ────────────────────────────────────────────────────► │ :8000            │
│  client)    │     "authorization_servers": [keycloak realm]         │ (resource server)│
│             │                                                       └──────────────────┘
│             │  3. discovery + dynamic client registration  ┌──────────────────┐
│             │ ───────────────────────────────────────────► │ Keycloak :8080   │
│             │  4. browser → user logs in (alex/alex123)    │ realm "mcp"      │
│             │  5. token request (PKCE)                     │ (authorization   │
│             │ ◄─────────────── access token (JWT) ──────── │  server)         │
│             │                                              └──────────────────┘
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
| Authorization server metadata at app origin (RFC 8414, legacy MCP 2025-03-26 discovery) | `AuthConfig(custom_oauth_metadata=...)` in `app/main.py` |
| Real AS metadata, login UI, token issuance, PKCE | Keycloak (`keycloak/realm-mcp.json`) |
| Dynamic client registration (RFC 7591) | App proxy `POST /oauth/register` → Keycloak `clients-registrations/openid-connect` |
| Token audience binding (RFC 8707 substitute) | Keycloak audience mapper in realm-default scope `hello-mcp-claims` |

## Keycloak realm notes (`keycloak/realm-mcp.json`)

- **Realm `mcp`** is imported automatically on container start (`--import-realm`).
- **Anonymous dynamic client registration** is enabled via the Trusted Hosts policy:
  host verification is off, but registered client redirect URIs must point at
  `localhost`/`127.0.0.1`. There is deliberately no "Allowed Client Scopes" registration
  policy — it rejected MCP clients' `scope` requests even for whitelisted scopes.
  This is sandbox-grade; real deployments restrict registration further.
- **Registration goes through the app's proxy** (`POST /oauth/register` in
  `app/main.py`), not straight to Keycloak. Keycloak's DCR sets a new client's scopes to
  exactly what the registration requested (wiping realm defaults), and its authorize
  endpoint hard-rejects requested-but-unassigned scopes. MCP clients register with one
  scope set but authorize with `offline_access` added — so the proxy rewrites the
  registration scope to the full set. The `profile` scope carries the audience + identity
  mappers, so dynamically registered clients mint valid tokens with no manual patching.
  This is the same trick fastapi-mcp's `setup_proxies=True` does for Auth0.
- **Audience mapper**: the realm-default client scope `hello-mcp-claims` stamps
  `aud: http://localhost:8000/mcp` plus `sub`/`preferred_username`/`email` claims into every
  access token, so the resource server can verify tokens were minted *for it*.
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

- Serve everything over HTTPS (OAuth endpoints require it for non-localhost).
- Replace the sandbox Keycloak (dev mode, in-memory-ish DB, open DCR) with a hardened
  instance or a hosted IdP (Auth0 etc. — fastapi-mcp has `setup_proxies=True` for providers
  without dynamic client registration).
- Pin down the Trusted Hosts registration policy and turn consent screens back on.
