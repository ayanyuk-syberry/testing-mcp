"""Token handling for the FastMCP flavour of the resource server.

Same job as ``app/auth.py``, expressed against the MCP SDK's auth machinery
instead of a FastAPI dependency. The SDK calls ``TokenVerifier.verify_token``
for every request to ``/mcp``; returning ``None`` makes it answer 401 with a
``WWW-Authenticate`` header pointing at the protected-resource metadata (the
metadata route is auto-served because ``resource_server_url`` is set on the
server's ``AuthSettings`` — see ``fastmcp_app/main.py``).

Like ``app/auth.py``, this trusts oauth2-proxy: real verification (signature,
issuer, audience) happened at the proxy on :8000, so here the forwarded JWT is
only decoded to recover the claims. The shared ``decode_forwarded_claims``
helper is imported from ``app.auth`` on purpose — this is the *same*
``hello-mcp`` resource server, just a second implementation, and the
trust-the-proxy decision stays single-sourced.
"""

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.auth import AUDIENCE, decode_forwarded_claims


class KeycloakTokenVerifier(TokenVerifier):
    """Trusts oauth2-proxy: decodes the forwarded, already-verified JWT."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = decode_forwarded_claims(token)
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
