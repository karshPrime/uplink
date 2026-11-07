from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from uplink import audit

router = APIRouter()


def creation(request: Request):
    return request.app.state.creation


class CreateProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    fork: str | None = None
    repo: str | None = None
    character: str


@router.post("/projects")
def create_project(request: Request, body: CreateProject):
    audit.note(
        request,
        action="create",
        project=body.name,
        repo=body.fork or body.repo,
        character=body.character,
    )
    job = creation(request).submit(body.name, body.fork, body.repo, body.character)
    audit.note(request, job=job.id)
    return JSONResponse(
        job.as_dict(), status_code=202, headers={"Location": f"/jobs/{job.id}"}
    )


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    return creation(request).get(job_id).as_dict()
