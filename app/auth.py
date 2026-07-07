"""Token handling for the MCP resource server, oauth2-proxy edition.

oauth2-proxy (the docker-compose service on the public :8000) now owns all
token verification: signature against Keycloak's JWKS, issuer, and audience
(``--skip-jwt-bearer-tokens`` + ``--oidc-extra-audiences``). This app runs
behind it on :8001 and FULLY TRUSTS the proxy: it decodes the forwarded JWT
*without* verifying it, purely to recover the claims for handlers and tools.

Trust boundary caveat: anything that reaches uvicorn directly on :8001
bypasses authentication entirely. Acceptable for this localhost sandbox,
never for real hosting — there the app port must be reachable only by the
proxy (network policy, mTLS, or a shared secret header).
"""

import os

import jwt
from fastapi import HTTPException, Request

KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://localhost:8080/realms/mcp")
# The PROXY's public URL — the app itself listens on :8001 behind it.
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
# Tokens must be minted *for this server* (audience binding, RFC 8707) —
# the same value the Keycloak audience mapper stamps into access tokens.
# The audience check itself now happens in oauth2-proxy (--oidc-extra-audiences).
AUDIENCE = f"{BASE_URL}/mcp"


def decode_forwarded_claims(token: str) -> dict:
    """Decode the proxy-verified JWT without re-verifying it (trust the proxy).

    Shared by both server flavours (``fastmcp_app/auth.py`` imports this) so
    the trust-the-proxy decision lives in exactly one place.
    """
    return jwt.decode(token, options={"verify_signature": False})


def _unauthorized(detail: str) -> HTTPException:
    # RFC 9728 §5.1: the 401 tells the client where to find our protected
    # resource metadata, which is what kicks off the client's OAuth discovery.
    # Behind the proxy this mostly fires only on direct :8001 access —
    # missing/invalid tokens through the proxy are 401'd by oauth2-proxy itself.
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

    try:
        claims = decode_forwarded_claims(auth_header.removeprefix("Bearer "))
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"Malformed token: {exc}")

    request.state.user = claims
    return claims
