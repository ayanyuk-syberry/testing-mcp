"""Token verification for the FastMCP flavour of the resource server.

Same job as ``app/auth.py``, expressed against the MCP SDK's auth machinery
instead of a FastAPI dependency. The SDK calls ``TokenVerifier.verify_token``
for every request to ``/mcp``; returning ``None`` makes it answer 401 with a
``WWW-Authenticate`` header pointing at the protected-resource metadata (the
metadata route is auto-served because ``resource_server_url`` is set on the
server's ``AuthSettings`` — see ``fastmcp_app/main.py``).

The issuer and audience are imported from ``app.auth`` on purpose: this is the
*same* ``hello-mcp`` resource server (same ``http://localhost:8000/mcp``
audience, same Keycloak realm), just a second implementation. Keeping the
load-bearing strings single-sourced means there's still only one place they live.
"""

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.auth import AUDIENCE, KEYCLOAK_ISSUER

# PyJWKClient caches Keycloak's public signing keys (rotated keys re-fetch automatically).
_jwks_client = PyJWKClient(f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs")


class KeycloakTokenVerifier(TokenVerifier):
    """Validate Keycloak-issued JWTs (signature + issuer + audience binding)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=KEYCLOAK_ISSUER,
                audience=AUDIENCE,
            )
        except jwt.PyJWTError:
            # The SDK turns None into a 401 with the right WWW-Authenticate header.
            return None

        return AccessToken(
            token=token,
            client_id=claims.get("azp", "unknown"),
            scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"),
            resource=AUDIENCE,
            subject=claims["sub"],
            claims=claims,
        )
