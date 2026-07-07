# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A learning sandbox for MCP (Model Context Protocol) servers. A FastAPI app exposes REST
endpoints as MCP tools over streamable HTTP (via `fastapi-mcp`), protected with OAuth 2.1
per the MCP authorization spec, with Keycloak as the authorization server using CIMD
(Client ID Metadata Documents, SEP-991) for client identity, and oauth2-proxy as the
token-verifying gatekeeper on the public port. The git history is part of the lesson: it
traces no-auth → DCR-with-registration-proxy → CIMD → oauth2-proxy-in-front; read it
before re-introducing patterns that were deliberately removed.

## Commands

```bash
# Keycloak (authorization server, :8080) + oauth2-proxy (gatekeeper, public :8000) —
# realm "mcp" auto-imports on start, takes ~30s
docker compose up -d

# The API / MCP server (Python 3.13, uv-managed) — :8001, BEHIND the proxy
uv run uvicorn app.main:app --reload --port 8001

# Alternative implementation of the SAME server using the MCP SDK's FastMCP instead of
# fastapi-mcp (same realm/audience/tools; run INSTEAD of app.main — it takes port 8001 too)
uv run uvicorn fastmcp_app.main:app --reload --port 8001

# Register in Claude Code (auth via /mcp → Authenticate; test user alex/alex123)
claude mcp add --transport http hello-mcp http://localhost:8000/mcp

# Alternative: static pre-registered client instead of CIMD (the `claude-code` client in
# the realm file; --callback-port must match its registered redirect URI port)
claude mcp add --transport http hello-mcp-static http://localhost:8000/mcp \
  --client-id claude-code --callback-port 8765

# Headless auth for testing (password grant via the test-cli client)
TOKEN=$(curl -s -X POST http://localhost:8080/realms/mcp/protocol/openid-connect/token \
  -d 'grant_type=password&client_id=test-cli&username=alex&password=alex123&scope=openid' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
curl -s http://localhost:8000/me -H "Authorization: Bearer $TOKEN"
```

- This machine's shell exports `UV_DEFAULT_INDEX` pointing at a private CodeArtifact
  mirror whose token expires; when uv commands 401, prefix them with
  `UV_DEFAULT_INDEX=https://pypi.org/simple` (applies to `uv run` too — it re-validates
  the lockfile).
- Changes to `keycloak/realm-mcp.json` require `docker compose down && docker compose up -d`
  — a plain restart keeps the container DB and the import is skipped (`IGNORE_EXISTING`).
- Keycloak admin console: http://localhost:8080 (admin/admin). Debugging auth failures:
  `docker logs testing-mcp-keycloak-1` — client-policy trace logging is enabled in
  docker-compose.yml. Proxy-side failures (401s on valid-looking tokens, 302s where a
  401 was expected, 502s): `docker logs testing-mcp-oauth2-proxy-1`.
- There are no tests or linters configured.

## Architecture

Four parties, strict role separation (the spec's core lesson — keep it that way):

- **oauth2-proxy is the token-verifying gatekeeper** (docker-compose service, owns the
  public :8000, upstreams to uvicorn on :8001 via `host.docker.internal`). It verifies
  every bearer JWT — signature via Keycloak's JWKS (fetched in-network at
  `keycloak:8080`), issuer, audience — using `--skip-jwt-bearer-tokens` +
  `--oidc-extra-audiences`. API paths (`^/mcp`, `^/me`, `^/jobs`) get a **bare 401**
  (no `resource_metadata` hint) instead of a login redirect; `/.well-known/` and
  `/hello` pass through unauthenticated (Claude Code's discovery chain probes the
  well-known paths itself, so the missing 401 hint doesn't matter). OIDC discovery is
  off (`--skip-oidc-discovery`) because browser-facing URLs are `localhost:8080` while
  in-network ones are `keycloak:8080` — the four endpoint URLs are set explicitly.
- **`app/` is a resource server that TRUSTS the proxy.** It never logs anyone in and
  serves no authorization-server metadata. `app/auth.py:verify_token` no longer verifies
  signatures — it decodes the proxy-forwarded JWT (`decode_forwarded_claims`, the
  single-sourced helper both flavours use) and 401s only when the header is missing.
  Direct requests to :8001 therefore bypass auth entirely — known sandbox trade-off,
  don't "fix" it by re-adding JWKS verification without asking. The RFC 9728 metadata
  route is still served by the app (`app/main.py`) and reached through the proxy.
  Everything else — discovery, CIMD client resolution, login, consent, PKCE, token
  issuance, refresh — happens at Keycloak.
- **`fastmcp_app/` is the same resource server, reimplemented on the MCP SDK's
  `FastMCP`** (not fastapi-mcp). Same realm, same `http://localhost:8000/mcp` audience,
  same tools — it exists as a side-by-side comparison and is run *instead of* `app.main`
  (same port). It reuses the load-bearing constants and the job-pattern core from `app/`
  (`fastmcp_app/auth.py` imports `AUDIENCE`/`decode_forwarded_claims` from `app.auth`;
  `fastmcp_app/main.py` imports the `Job`/`_run_job`/`_jobs` core from `app.jobs`), so
  keep that logic single-sourced — change it in `app/`, not by forking. See the
  "fastmcp-specifics" subsection below.
- **Keycloak config lives entirely in `keycloak/realm-mcp.json`** and must stay
  reproducible from `docker compose up` alone; never leave manual admin-console changes
  as the only copy. CIMD is experimental (`--features=cimd`, Keycloak 26.6) and is
  enabled by the `clientProfiles`/`clientPolicies` blocks in the realm file.
- **Client identity is CIMD**: the MCP client's `client_id` is a URL (Claude Code uses
  `https://claude.ai/oauth/claude-code-client-metadata`); Keycloak fetches metadata from
  it on demand. No client registration state. Anonymous DCR still exists as legacy
  fallback but has a known Keycloak quirk (registration wipes realm-default scopes,
  then authorize-time scope validation hard-rejects) — that's why the DCR era needed a
  registration proxy; don't resurrect it. The realm also carries a static `claude-code`
  client demonstrating the third registration model (manual pre-registration, what
  CIMD/DCR exist to avoid) — connect with `--client-id claude-code --callback-port 8765`.

### fastapi-mcp specifics

- A REST endpoint's `operation_id` becomes the MCP tool name; docstring +
  `response_model` become the tool description/schema the LLM sees — always set all
  three explicitly.
- `FastApiMCP(app, ...)` must be instantiated **after** all routes are declared, and
  `mount_http()` serves streamable HTTP at `/mcp`.
- Auth plugs in via `AuthConfig(dependencies=[Depends(verify_token)])`; fastapi-mcp
  forwards the `Authorization` header from the MCP request into internal tool
  invocations, so protected endpoints (like `/me`) can read the caller's token claims.

### fastmcp specifics (the MCP SDK's `FastMCP`, `fastmcp_app/`)

- Tools are declared natively with `@mcp.tool(name=...)`; the docstring becomes the tool
  description and the return type's schema (a Pydantic model) becomes the output schema.
- Auth is a `TokenVerifier` (`fastmcp_app/auth.py:KeycloakTokenVerifier`) whose
  `verify_token` returns an `AccessToken` (with the decoded JWT in `.claims`) or `None`.
  Pass it as `token_verifier=` plus `auth=AuthSettings(issuer_url=..., resource_server_url=
  AUDIENCE, required_scopes=None)`. Setting `resource_server_url` makes the SDK
  auto-serve the RFC 9728 metadata route (`/.well-known/oauth-protected-resource/mcp`
  only — not the bare-root variant `app/main.py` also serves) and enforce bearer auth on
  the whole `/mcp` endpoint. The whole endpoint is protected — no per-tool public/private
  split.
- Inside a tool, read the caller with `get_access_token()` from
  `mcp.server.auth.middleware.auth_context` (`.claims`, `.subject`). Raise
  `mcp.server.fastmcp.exceptions.ToolError` for tool-level errors.
- `json_response=True` matches the buffered single-response transport the job pattern
  assumes. Serve it under uvicorn via the module-level `app = mcp.streamable_http_app()`.

### Load-bearing strings (change all together or break auth)

- `http://localhost:8000/mcp` is the token **audience**, appearing in: `AUDIENCE` in
  `app/auth.py`, `OAUTH2_PROXY_OIDC_EXTRA_AUDIENCES` in docker-compose.yml (where the
  check is actually enforced now), the `included.custom.audience` of the audience
  mappers in the realm file, and the URL registered in Claude Code. Use `localhost`,
  not `127.0.0.1` — they are distinct strings to both audience checks and Keycloak's
  trusted-domain matching.
- Port **8001** couples `OAUTH2_PROXY_UPSTREAMS` (`http://host.docker.internal:8001`
  in docker-compose.yml) to the uvicorn `--port` — change both or the proxy 502s.
- `KC_HOSTNAME: http://localhost:8080` (docker-compose.yml) pins the Keycloak issuer;
  oauth2-proxy's `OIDC_ISSUER_URL` must match it byte-for-byte. Without it, tokens
  redeemed in-network at `keycloak:8080` carry the wrong `iss` and the proxy's browser
  cookie flow fails (the bearer path keeps working — easy to miss).
- CIMD trusted-domain lists (two places in the realm file, executor and condition,
  with **different** JSON keys: `cimd-allow-permitted-domains` vs
  `client-id-uri-allow-permitted-domains`) validate the client metadata URL's domain
  **and** redirect-URI hosts; `127.0.0.1` and `localhost` must both be listed. No `*`
  match-all; failures appear as `client_not_found` / "domain not allowed" — grep the
  Keycloak logs for `not trusted domain`.

### Keycloak realm-import gotchas (learned the hard way)

File-imported realms do **not** get the built-in client scopes (`profile`, `email`,
`basic`) or full default-role wiring that admin-created realms get. Hence the realm file
explicitly defines `profile`/`email` scopes, carries the audience + identity claim
mappers in both `hello-mcp-claims` (realm-default) and `profile` (for clients that lose
realm defaults), and grants the test user the `offline_access` realm role — without that
role, token exchange fails with "Offline tokens not allowed" because MCP clients request
the `offline_access` scope for refresh tokens.
