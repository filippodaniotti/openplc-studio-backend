import io
import os
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse

from plc_platform_backend.assets.assets_models import TestbenchNodeDepth
from plc_platform_backend.runs.runs_models import Run, RunCreateDto
from plc_platform_backend.runs.runs_service import RunsService, get_runs_service

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    dependencies=[],
    responses={404: {"description": "Not found"}},
)


@router.post(
    "",
    status_code=201,
)
async def create_run(
    run: RunCreateDto,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> Run:
    return await runs_service.save_run(run)


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> Run:
    return await runs_service.find_by_id(run_id)
