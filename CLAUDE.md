# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A learning sandbox for MCP (Model Context Protocol) servers. A FastAPI app exposes REST
endpoints as MCP tools over streamable HTTP (via `fastapi-mcp`), protected with OAuth 2.1
per the MCP authorization spec, with Keycloak as the authorization server using CIMD
(Client ID Metadata Documents, SEP-991) for client identity. The git history is part of
the lesson: it traces no-auth → DCR-with-registration-proxy → CIMD; read it before
re-introducing patterns that were deliberately removed.

## Commands

```bash
# Keycloak (authorization server) — realm "mcp" auto-imports on start, takes ~30s
docker compose up -d

# The API / MCP server (Python 3.13, uv-managed)
uv run uvicorn app.main:app --reload --port 8000

# Register in Claude Code (auth via /mcp → Authenticate; test user alex/alex123)
claude mcp add --transport http hello-mcp http://localhost:8000/mcp

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
  docker-compose.yml.
- There are no tests or linters configured.

## Architecture

Three parties, strict role separation (the spec's core lesson — keep it that way):

- **`app/` is a pure OAuth resource server.** It never logs anyone in and serves no
  authorization-server metadata. `app/auth.py:verify_token` validates JWTs against
  Keycloak's JWKS (signature + issuer + audience) and returns 401 with a
  `WWW-Authenticate` header pointing at `/.well-known/oauth-protected-resource`
  (RFC 9728, served in `app/main.py`), which names Keycloak as the authorization server.
  Everything else — discovery, CIMD client resolution, login, consent, PKCE, token
  issuance, refresh — happens at Keycloak.
- **Keycloak config lives entirely in `keycloak/realm-mcp.json`** and must stay
  reproducible from `docker compose up` alone; never leave manual admin-console changes
  as the only copy. CIMD is experimental (`--features=cimd`, Keycloak 26.6) and is
  enabled by the `clientProfiles`/`clientPolicies` blocks in the realm file.
- **Client identity is CIMD**: the MCP client's `client_id` is a URL (Claude Code uses
  `https://claude.ai/oauth/claude-code-client-metadata`); Keycloak fetches metadata from
  it on demand. No client registration state. Anonymous DCR still exists as legacy
  fallback but has a known Keycloak quirk (registration wipes realm-default scopes,
  then authorize-time scope validation hard-rejects) — that's why the DCR era needed a
  registration proxy; don't resurrect it.

### fastapi-mcp specifics

- A REST endpoint's `operation_id` becomes the MCP tool name; docstring +
  `response_model` become the tool description/schema the LLM sees — always set all
  three explicitly.
- `FastApiMCP(app, ...)` must be instantiated **after** all routes are declared, and
  `mount_http()` serves streamable HTTP at `/mcp`.
- Auth plugs in via `AuthConfig(dependencies=[Depends(verify_token)])`; fastapi-mcp
  forwards the `Authorization` header from the MCP request into internal tool
  invocations, so protected endpoints (like `/me`) can read the caller's token claims.

### Load-bearing strings (change all together or break auth)

- `http://localhost:8000/mcp` is the token **audience**, appearing in: `AUDIENCE` in
  `app/auth.py`, the `included.custom.audience` of the audience mappers in the realm
  file, and the URL registered in Claude Code. Use `localhost`, not `127.0.0.1` — they
  are distinct strings to both audience checks and Keycloak's trusted-domain matching.
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
