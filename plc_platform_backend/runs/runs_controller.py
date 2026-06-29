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
from plc_platform_backend.runs.runs_models import Run, RunCreateDto, RunConfigDto, RunConfigValidationError

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

@router.get("/{run_id}/config/export")
async def export_run_config(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> StreamingResponse:
    config_json: str = await runs_service.export_run_config(run_id)
    return StreamingResponse(
        io.BytesIO(config_json.encode("utf-8")),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=run_{run_id}_config.json"},
    )


@router.get("/{run_id}/assets/{depth}")
async def get_run_assets_paths(
    run_id: str,
    depth: int,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
    background_tasks: BackgroundTasks,
) -> FileResponse:
    tar_archive: io.BytesIO = await runs_service.get_assets_tar_by_depth(
        run_id, TestbenchNodeDepth(depth)
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp:
        tmp.write(tar_archive.getvalue())
        tmp.flush()
        tmp = tmp.name

    background_tasks.add_task(os.unlink, tmp)

    return FileResponse(
        tmp,
        media_type="application/octet-stream",
        filename=f"run_{run_id}_assets_depth_{depth}.tar",
        background=background_tasks,
    )


@router.get("")
async def get_all_runs(
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> list[Run]:
    return await runs_service.get_all()



#controller for validating run config
@router.post("/config/validate")
async def validate_run_config(
    config: RunConfigDto,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> RunConfigDto:
    return await runs_service.validate_run_config(config)