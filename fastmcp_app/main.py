"""The `hello-mcp` resource server, implemented with the MCP SDK's FastMCP.

This is a second implementation of the *same* server as `app/` — same Keycloak
realm, same `http://localhost:8000/mcp` audience — but MCP-first instead of
REST-first:

- `app/` (fastapi-mcp): REST endpoints become tools via their `operation_id`,
  and auth is a FastAPI `Depends(verify_token)` dependency.
- here (FastMCP): tools are declared natively with `@mcp.tool()`, and auth is the
  SDK's `TokenVerifier` + `AuthSettings`. Setting `resource_server_url` makes the
  SDK auto-serve the RFC 9728 metadata route and enforce bearer auth on `/mcp`.

Run this *instead of* the FastAPI app (it takes the same port 8000):

    uv run uvicorn fastmcp_app.main:app --reload --port 8000
"""

import asyncio
from uuid import uuid4

import boto3
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

from app.auth import AUDIENCE, KEYCLOAK_ISSUER
# The job-pattern core is framework-agnostic — reuse it rather than reimplement.
# (Shares the in-process `_jobs` dict with app/jobs.py, which is harmless: the two
# servers take the same port and so never run at the same time.)
from app.jobs import (
    POLL_INTERVAL_SECONDS,
    Job,
    JobResultResponse,
    JobStatus,
    JobStatusResponse,
    StartJobResponse,
    _jobs,
    _run_job,
)
from fastmcp_app.auth import KeycloakTokenVerifier


class HelloResponse(BaseModel):
    message: str


class WhoAmIResponse(BaseModel):
    username: str
    email: str | None = None
    subject: str
    arn: str


mcp = FastMCP(
    name="hello-mcp",
    token_verifier=KeycloakTokenVerifier(),
    auth=AuthSettings(
        issuer_url=KEYCLOAK_ISSUER,
        # Resource identifier + base for the auto-served RFC 9728 metadata route.
        resource_server_url=AUDIENCE,
        # Match the fastapi-mcp flavour: validate the token, don't gate on scope.
        required_scopes=None,
    ),
    # Single buffered JSON response per call (no SSE stream) — the transport mode
    # the job pattern below is designed around.
    json_response=True,
)


@mcp.tool(name="hello_world")
def hello(name: str = "world") -> HelloResponse:
    """Say hello to someone."""
    return HelloResponse(message=f"Hello, {name}!")


@mcp.tool(name="whoami")
def whoami() -> WhoAmIResponse:
    """Return the identity of the logged-in user, taken from the OAuth access token,
    plus the AWS caller-identity ARN of the assumed PersonalAccess role."""
    claims = get_access_token().claims
    # "personal" is a machine-local profile: SSO role PersonalAccess in 905418478567.
    # Requires a valid `aws sso login` session.
    sts = boto3.Session(profile_name="personal").client("sts")
    return WhoAmIResponse(
        username=claims.get("preferred_username", "<unknown>"),
        email=claims.get("email"),
        subject=claims["sub"],
        arn=sts.get_caller_identity()["Arn"],
    )


# --- Job pattern (see app/jobs.py for the why) -----------------------------------

def _get_owned_job(job_id: str) -> Job:
    job = _jobs.get(job_id)
    if job is None:
        raise ToolError(f"No job with id {job_id}")
    if job.owner != get_access_token().subject:
        raise ToolError("Job belongs to another user")
    return job


@mcp.tool(name="start_job")
async def start_job(duration_seconds: int = 120) -> StartJobResponse:
    """Start a long-running job (it sleeps for duration_seconds, 120 by default).

    Returns immediately with a job_id. The job runs in the background. After
    starting, keep polling get_job_status every 15 seconds until the status is
    'completed' (don't ask the user first), then call get_job_result. Do not poll
    more often than every 15 seconds.
    """
    if not 5 <= duration_seconds <= 600:
        raise ToolError("duration_seconds must be between 5 and 600")
    job_id = str(uuid4())
    job = Job(owner=get_access_token().subject, duration=duration_seconds)
    job.task = asyncio.create_task(_run_job(job))
    _jobs[job_id] = job
    return StartJobResponse(
        job_id=job_id,
        status=job.status,
        duration_seconds=duration_seconds,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )


@mcp.tool(name="get_job_status")
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Check on a job started with start_job. Returns instantly, never blocks.

    While the status is 'running', wait at least 15 seconds before polling again.
    Once it is 'completed', fetch the outcome with get_job_result.
    """
    job = _get_owned_job(job_id)
    return JobStatusResponse(
        job_id=job_id,
        status=job.status,
        progress=job.progress,
        message=f"Job is {job.status.value} ({job.progress}% of {job.duration}s).",
    )


@mcp.tool(name="get_job_result")
async def get_job_result(job_id: str) -> JobResultResponse:
    """Fetch the result of a job once get_job_status reports it 'completed'.

    Calling this while the job is still running is an error, not a way to wait for
    it — keep polling get_job_status instead.
    """
    job = _get_owned_job(job_id)
    if job.status is not JobStatus.completed:
        raise ToolError(
            f"Job is {job.status.value} ({job.progress}%), no result yet. "
            f"Poll get_job_status until it is 'completed'."
        )
    return JobResultResponse(job_id=job_id, status=job.status, result=job.result)


@mcp.tool(name="cancel_job")
async def cancel_job(job_id: str) -> JobStatusResponse:
    """Cancel a running job started with start_job. Completed jobs cannot be cancelled."""
    job = _get_owned_job(job_id)
    if job.status is not JobStatus.running:
        raise ToolError(f"Job is already {job.status.value}")
    if job.task is not None:
        job.task.cancel()
    job.status = JobStatus.cancelled
    return JobStatusResponse(
        job_id=job_id,
        status=job.status,
        progress=job.progress,
        message="Job cancelled.",
    )


# Streamable-HTTP ASGI app, served at /mcp — run under uvicorn like the FastAPI flavour.
app = mcp.streamable_http_app()
