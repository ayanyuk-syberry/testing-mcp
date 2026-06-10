"""Job pattern for long-running MCP tool calls.

This server's MCP transport (streamable HTTP in JSON-response mode) answers each
tool call with a single buffered response — there is no channel for progress
notifications and no way to keep a minutes-long call alive past client/proxy
timeouts. So instead of holding the call open, a long operation is split into
fast tools: start_job returns a job_id immediately and the work runs in the
background; the client polls get_job_status and fetches get_job_result when done.
Each poll is a quick, freshly-authenticated request, so token expiry mid-job is
a non-issue.

The "work" here is just asyncio.sleep. Jobs live in a process-local dict and are
lost on restart (uvicorn --reload included) — fine for a sandbox, a real server
would use a database or task queue.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import verify_token

POLL_INTERVAL_SECONDS = 15

router = APIRouter()


class JobStatus(str, Enum):
    running = "running"
    completed = "completed"
    cancelled = "cancelled"


class StartJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    duration_seconds: int
    poll_interval_seconds: int


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int
    message: str


class JobResultResponse(BaseModel):
    job_id: str
    status: JobStatus
    result: str


@dataclass
class Job:
    owner: str  # `sub` claim of the token that started the job
    duration: int
    status: JobStatus = JobStatus.running
    started_at: float = field(default_factory=time.monotonic)
    result: str | None = None
    # Strong reference to the worker — also lets cancel_job stop it.
    task: asyncio.Task | None = None

    @property
    def progress(self) -> int:
        if self.status is JobStatus.completed:
            return 100
        elapsed = time.monotonic() - self.started_at
        return min(int(elapsed / self.duration * 100), 99)


_jobs: dict[str, Job] = {}


async def _run_job(job: Job) -> None:
    await asyncio.sleep(job.duration)
    job.status = JobStatus.completed
    job.result = f"Slept {job.duration} seconds without interruption. Riveting work."


def _get_owned_job(job_id: str, claims: dict) -> Job:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    if job.owner != claims["sub"]:
        raise HTTPException(status_code=403, detail="Job belongs to another user")
    return job


@router.post("/jobs", operation_id="start_job", response_model=StartJobResponse)
async def start_job(
    duration_seconds: int = Query(default=120, ge=5, le=600),
    claims: dict = Depends(verify_token),
) -> StartJobResponse:
    """Start a long-running job (it sleeps for duration_seconds, 120 by default).

    Returns immediately with a job_id. The job runs in the background; poll
    get_job_status no more often than every 15 seconds, and call get_job_result
    only once the status is 'completed'.
    """
    job_id = str(uuid4())
    job = Job(owner=claims["sub"], duration=duration_seconds)
    job.task = asyncio.create_task(_run_job(job))
    _jobs[job_id] = job
    return StartJobResponse(
        job_id=job_id,
        status=job.status,
        duration_seconds=duration_seconds,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )


@router.get(
    "/jobs/{job_id}/status",
    operation_id="get_job_status",
    response_model=JobStatusResponse,
)
async def get_job_status(
    job_id: str, claims: dict = Depends(verify_token)
) -> JobStatusResponse:
    """Check on a job started with start_job. Returns instantly, never blocks.

    While the status is 'running', wait at least 15 seconds before polling again.
    Once it is 'completed', fetch the outcome with get_job_result.
    """
    job = _get_owned_job(job_id, claims)
    return JobStatusResponse(
        job_id=job_id,
        status=job.status,
        progress=job.progress,
        message=f"Job is {job.status.value} ({job.progress}% of {job.duration}s).",
    )


@router.get(
    "/jobs/{job_id}/result",
    operation_id="get_job_result",
    response_model=JobResultResponse,
)
async def get_job_result(
    job_id: str, claims: dict = Depends(verify_token)
) -> JobResultResponse:
    """Fetch the result of a job once get_job_status reports it 'completed'.

    Calling this while the job is still running is an error (HTTP 409), not a
    way to wait for it — keep polling get_job_status instead.
    """
    job = _get_owned_job(job_id, claims)
    if job.status is not JobStatus.completed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is {job.status.value} ({job.progress}%), no result yet. "
                f"Poll get_job_status until it is 'completed'."
            ),
        )
    return JobResultResponse(job_id=job_id, status=job.status, result=job.result)


@router.delete("/jobs/{job_id}", operation_id="cancel_job", response_model=JobStatusResponse)
async def cancel_job(
    job_id: str, claims: dict = Depends(verify_token)
) -> JobStatusResponse:
    """Cancel a running job started with start_job. Completed jobs cannot be cancelled."""
    job = _get_owned_job(job_id, claims)
    if job.status is not JobStatus.running:
        raise HTTPException(
            status_code=409, detail=f"Job is already {job.status.value}"
        )
    if job.task is not None:
        job.task.cancel()
    job.status = JobStatus.cancelled
    return JobStatusResponse(
        job_id=job_id,
        status=job.status,
        progress=job.progress,
        message="Job cancelled.",
    )
