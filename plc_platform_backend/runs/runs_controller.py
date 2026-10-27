import io
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from plc_platform_backend.assets.assets_models import RunArtifactKind
from plc_platform_backend.modules.modules_service import (
    ModuleService,
    get_modules_service,
)
from plc_platform_backend.runs.runs_models import (
    Run,
    RunCreateDto,
    RunConfigDto,
    RunConfigValidationError,
    RunDashboardSummary,
    RunPage,
    RunSortField,
    RunStatus,
    SortDirection,
)
from plc_platform_backend.runs.runs_service import (
    RunArtifactConversionError,
    RunArtifactsNotFoundError,
    RunArtifactsUnavailableError,
    RunNotDeletableError,
    RunNotExecutableError,
    RunNotRetryableError,
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

_ARCHIVE_CHUNK_SIZE = 1024 * 1024


def _iter_buffer_chunks(buffer: io.BytesIO) -> Iterator[bytes]:
    """Stream binary buffers in fixed chunks instead of newline-delimited chunks."""
    try:
        while chunk := buffer.read(_ARCHIVE_CHUNK_SIZE):
            yield chunk
    finally:
        buffer.close()


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


@router.post("/{run_id}/retry", status_code=202)
async def retry_run(
    run_id: str,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
    modules_service: Annotated[ModuleService, Depends(get_modules_service)],
) -> Run:
    try:
        return await runs_service.retry_run(run_id, modules_service)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RunNotRetryableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RunPreparationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RunQueueError as error:
        detail: str | dict[str, str] = str(error)
        if error.run_id:
            detail = {"message": str(error), "run_id": error.run_id}
        raise HTTPException(status_code=503, detail=detail) from error


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


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
) -> RunDashboardSummary:
    return await runs_service.get_dashboard_summary()


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


@router.get(
    "/{run_id}/artifacts/{kind}/archive",
    summary="Download a run artifact archive",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Tar archive containing the requested run artifacts",
            "content": {"application/x-tar": {}},
        },
        404: {"description": "Run or expected artifact not found"},
        409: {"description": "Run artifacts are not available yet"},
        422: {"description": "Invalid kind or artifact conversion failure"},
    },
    description=(
        "Returns a tar archive for the named artifact kind. Original and "
        "reconstructed tracks remain WAV files. Sample-mask NumPy files are "
        "converted to JSON arrays. Pickled output analyses are converted to JSON; "
        "SimpleCalculator errors are transposed with NaN values replaced by zero; "
        "whole-track SimpleCalculator results are JSON numbers, and PEAQ results "
        "are represented as `[DI, ODG]`."
    ),
)
async def get_run_artifacts_archive(
    run_id: str,
    kind: RunArtifactKind,
    runs_service: Annotated[RunsService, Depends(get_runs_service)],
    ready_only: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    try:
        tar_archive = await runs_service.get_artifacts_archive(run_id, kind, ready_only=True) if ready_only else await runs_service.get_artifacts_archive(run_id, kind)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RunArtifactsUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RunArtifactsNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "One or more run artifacts were not found",
                "artifacts": error.archive_paths,
            },
        ) from error
    except RunArtifactConversionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    content_length = tar_archive.getbuffer().nbytes
    return StreamingResponse(
        _iter_buffer_chunks(tar_archive),
        media_type="application/x-tar",
        headers={
            "Content-Disposition": (
                f"attachment; filename=run_{run_id}_{kind.value}.tar"
            ),
            "Content-Length": str(content_length),
        },
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
