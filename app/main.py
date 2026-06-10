import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi_mcp import AuthConfig, FastApiMCP
from pydantic import BaseModel

from app.auth import AUDIENCE, BASE_URL, KEYCLOAK_ISSUER, verify_token


class HelloResponse(BaseModel):
    message: str


class WhoAmIResponse(BaseModel):
    username: str
    email: str | None = None
    subject: str


app = FastAPI(
    title="Hello MCP API",
    description="Experimental FastAPI service that doubles as an MCP server.",
)


@app.get("/hello", operation_id="hello_world", response_model=HelloResponse)
async def hello(name: str = "world") -> HelloResponse:
    """Say hello to someone."""
    return HelloResponse(message=f"Hello, {name}!")


@app.get("/me", operation_id="whoami", response_model=WhoAmIResponse)
async def whoami(claims: dict = Depends(verify_token)) -> WhoAmIResponse:
    """Return the identity of the logged-in user, taken from the OAuth access token."""
    return WhoAmIResponse(
        username=claims.get("preferred_username", "<unknown>"),
        email=claims.get("email"),
        subject=claims["sub"],
    )


# RFC 9728 protected resource metadata: tells MCP clients which authorization
# server protects this resource. Clients find this URL via the WWW-Authenticate
# header on a 401. Served both at the root and with the /mcp path suffix
# (clients derive the suffixed form from the resource URL per RFC 9728 §3).
# authorization_servers points at THIS app (not Keycloak directly) so that
# clients discover the registration proxy below; authorize/token still go
# straight to Keycloak via the metadata fastapi-mcp serves at this origin.
@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource_metadata() -> dict:
    return {
        "resource": AUDIENCE,
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "profile", "email"],
    }


# Registration proxy (the trick fastapi-mcp's setup_proxies does for Auth0).
# Keycloak's dynamic client registration sets the new client's scopes to exactly
# what the request asks for, and its authorize endpoint hard-rejects any requested
# scope the client wasn't assigned. MCP clients register with one scope set but
# authorize with another (adding offline_access), so we rewrite the registration
# to the full set the client will later request.
@app.post("/oauth/register", include_in_schema=False)
async def register_client_proxy(request: Request) -> JSONResponse:
    body = await request.json()
    body["scope"] = "openid profile email offline_access"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KEYCLOAK_ISSUER}/clients-registrations/openid-connect",
            json=body,
            headers={"Content-Type": "application/json"},
        )
    return JSONResponse(resp.json(), status_code=resp.status_code)


mcp = FastApiMCP(
    app,
    name="hello-mcp",
    auth_config=AuthConfig(
        # RFC 8414 authorization server metadata served from this app's origin.
        # Both discovery paths land here (modern clients via the RFC 9728 endpoint
        # above, legacy MCP 2025-03-26 clients directly). authorize/token point at
        # Keycloak; registration points at our proxy endpoint above.
        custom_oauth_metadata={
            "issuer": BASE_URL,
            "authorization_endpoint": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/auth",
            "token_endpoint": f"{KEYCLOAK_ISSUER}/protocol/openid-connect/token",
            "registration_endpoint": f"{BASE_URL}/oauth/register",
            "scopes_supported": ["openid", "profile", "email"],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
        },
        dependencies=[Depends(verify_token)],
    ),
)
mcp.mount_http()  # streamable-HTTP MCP endpoint at /mcp
