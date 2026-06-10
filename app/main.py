import boto3
from fastapi import Depends, FastAPI
from fastapi_mcp import AuthConfig, FastApiMCP
from pydantic import BaseModel

from app.auth import AUDIENCE, KEYCLOAK_ISSUER, verify_token
from app.jobs import router as jobs_router


class HelloResponse(BaseModel):
    message: str


class WhoAmIResponse(BaseModel):
    username: str
    email: str | None = None
    subject: str
    arn: str


app = FastAPI(
    title="Hello MCP API",
    description="Experimental FastAPI service that doubles as an MCP server.",
)


@app.get("/hello", operation_id="hello_world", response_model=HelloResponse)
async def hello(name: str = "world") -> HelloResponse:
    """Say hello to someone."""
    return HelloResponse(message=f"Hello, {name}!")


@app.get("/me", operation_id="whoami", response_model=WhoAmIResponse)
def whoami(claims: dict = Depends(verify_token)) -> WhoAmIResponse:
    """Return the identity of the logged-in user, taken from the OAuth access token,
    plus the AWS caller-identity ARN of the assumed PersonalAccess role."""
    # "personal" is a machine-local profile: SSO role PersonalAccess in 905418478567.
    # Requires a valid `aws sso login` session.
    sts = boto3.Session(profile_name="personal").client("sts")
    return WhoAmIResponse(
        username=claims.get("preferred_username", "<unknown>"),
        email=claims.get("email"),
        subject=claims["sub"],
        arn=sts.get_caller_identity()["Arn"],
    )


# Job-pattern endpoints (start_job / get_job_status / get_job_result / cancel_job)
# for long-running tool calls — must be included before FastApiMCP is instantiated.
app.include_router(jobs_router)


# RFC 9728 protected resource metadata: tells MCP clients which authorization
# server protects this resource. Clients find this URL via the WWW-Authenticate
# header on a 401, then talk to Keycloak directly — discovery, CIMD client
# resolution, login, and token issuance all happen there; this app is a pure
# resource server. Served both at the root and with the /mcp path suffix
# (clients derive the suffixed form from the resource URL per RFC 9728 §3).
@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource_metadata() -> dict:
    return {
        "resource": AUDIENCE,
        "authorization_servers": [KEYCLOAK_ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "profile", "email"],
    }


mcp = FastApiMCP(
    app,
    name="hello-mcp",
    auth_config=AuthConfig(dependencies=[Depends(verify_token)]),
)
mcp.mount_http()  # streamable-HTTP MCP endpoint at /mcp
