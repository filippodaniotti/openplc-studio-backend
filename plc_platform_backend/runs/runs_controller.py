import io
import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse

from plc_platform_backend.assets.assets_models import TestbenchNodeDepth
from plc_platform_backend.modules.modules_service import (
    ModuleService,
    get_modules_service,
)
from plc_platform_backend.runs.runs_models import (
    Run,
    RunCreateDto,
    RunConfigDto,
    RunConfigValidationError,
    RunPage,
    RunSortField,
    RunStatus,
    SortDirection,
)
from plc_platform_backend.runs.runs_service import (
    RunNotDeletableError,
    RunNotExecutableError,
    RunPreparationError,
    RunQueueError,
    RunsService,
    get_runs_service,
)

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
    modules_service: Annotated[ModuleService, Depends(get_modules_service)],
) -> Run:
    errors = await runs_service.validate_run_create(run, modules_service)
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[error.model_dump() for error in errors],
        )
    try:
        return await runs_service.save_run(run)
    except RunPreparationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{run_id}/execute", status_code=202)
async def execute_run(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> Run:
    try:
        return await runs_service.execute_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RunNotExecutableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RunQueueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> Run:
    return await runs_service.find_by_id(run_id)


@router.delete("/{run_id}", status_code=204)
async def delete_run(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> None:
    try:
        await runs_service.delete_run(run_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RunNotDeletableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/{run_id}/config/export")
async def export_run_config(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> StreamingResponse:
    config_json: str = await runs_service.export_run_config(run_id)
    return StreamingResponse(
        io.BytesIO(config_json.encode("utf-8")),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename=run_{run_id}_config.json"
        },
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
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
    search: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[list[RunStatus] | None, Query()] = None,
    sort_by: RunSortField = "created",
    sort_direction: SortDirection = "desc",
) -> RunPage:
    return await runs_service.get_page(
        page,
        page_size,
        search,
        status,
        sort_by,
        sort_direction,
    )


@router.post("/config/validate")
async def validate_run_config(
    config: RunConfigDto,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
    modules_service: Annotated[ModuleService, Depends(get_modules_service)],
) -> RunConfigDto:
    errors: list[RunConfigValidationError] = await runs_service.validate_run_config(
        config, modules_service
    )
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[error.model_dump() for error in errors],
        )
    return config
