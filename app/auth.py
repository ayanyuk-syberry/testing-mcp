"""Token validation for the MCP resource server (OAuth 2.1 tier).

The FastAPI app acts as an OAuth *resource server*: it never logs anyone in,
it only validates access tokens issued by the authorization server (Keycloak)
and rejects requests that don't carry a valid one.
"""

import os

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8080/realms/mcp")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
# Tokens must be minted *for this server* (audience binding, RFC 8707) —
# the same value the Keycloak audience mapper stamps into access tokens.
AUDIENCE = f"{BASE_URL}/mcp"

# PyJWKClient caches Keycloak's public signing keys (rotated keys re-fetch automatically).
_jwks_client = PyJWKClient(f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs")


def _unauthorized(detail: str) -> HTTPException:
    # RFC 9728 §5.1: the 401 tells the client where to find our protected
    # resource metadata, which is what kicks off the client's OAuth discovery.
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"'
            )
        },
    )


async def verify_token(request: Request) -> dict:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise _unauthorized("Missing bearer token")
    token = auth_header.removeprefix("Bearer ")

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=KEYCLOAK_ISSUER,
            audience=AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"Invalid token: {exc}")

    request.state.user = claims
    return claims
