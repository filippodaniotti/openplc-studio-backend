from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from plc_platform_backend.assets.assets_models import (
    OriginalTrackMetadata,
    TrackDeleteRequest,
    TrackDeleteResponse,
    TrackMetadata,
    TrackPage,
    TrackRunReferencePage,
)
from plc_platform_backend.assets.assets_service import (
    AssetsService,
    TrackInUseError,
    get_assets_service,
)
from plc_platform_backend.assets.track_storage import (
    InvalidTrackError,
    TrackAlreadyExistsError,
    TrackNotFoundError,
    iter_file,
)

router = APIRouter(
    prefix="/assets",
    tags=["assets"],
    dependencies=[],
    responses={404: {"description": "Not found"}},
)


@router.post("", status_code=201)
async def upload_assets(
    files: Annotated[list[UploadFile], File()],
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
):
    try:
        for file in files:
            await assets_service.upload_track(file, overwrite=True)
    except InvalidTrackError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await assets_service.get_all_original_track_filenames()


@router.get("/original-tracks")
async def get_original_tracks(
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
):
    return await assets_service.get_all_original_track_filenames()


@router.get("/original-tracks/metadata")
async def get_original_tracks_metadata(
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
) -> list[OriginalTrackMetadata]:
    return await assets_service.get_all_original_track_metadata()


@router.get("/tracks", response_model=TrackPage)
async def get_tracks(
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    search: str | None = None,
    sort_by: Literal["name", "size_bytes", "duration_seconds", "last_modified"] = "name",
    sort_direction: Literal["asc", "desc"] = "asc",
) -> TrackPage:
    return await assets_service.get_tracks_page(
        page, page_size, search, sort_by, sort_direction
    )


@router.post("/tracks", status_code=201, response_model=list[TrackMetadata])
async def upload_tracks(
    files: Annotated[list[UploadFile], File()],
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
    overwrite: bool = False,
) -> list[TrackMetadata]:
    filenames = [file.filename for file in files if file.filename]
    if not overwrite:
        existing = set(await assets_service.get_all_original_track_filenames())
        duplicates = {name for name in filenames if filenames.count(name) > 1}
        conflicts = sorted((set(filenames) & existing) | duplicates)
        if conflicts:
            for file in files:
                await file.close()
            raise HTTPException(
                status_code=409,
                detail={"code": "track_exists", "conflicts": conflicts},
            )

    uploaded: list[TrackMetadata] = []
    try:
        for file in files:
            uploaded.append(await assets_service.upload_track(file, overwrite=overwrite))
    except TrackAlreadyExistsError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "track_exists", "conflicts": [error.filename]},
        ) from error
    except InvalidTrackError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return uploaded


@router.delete("/tracks", response_model=TrackDeleteResponse)
async def delete_tracks(
    request: TrackDeleteRequest,
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
) -> TrackDeleteResponse:
    try:
        deleted = await assets_service.delete_tracks(request.names)
    except TrackNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidTrackError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except TrackInUseError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "track_in_use",
                "tracks": {
                    name: [
                        {
                            "id": str(run.id),
                            "name": run.name,
                            "status": run.status.value,
                        }
                        for run in runs
                    ]
                    for name, runs in error.references.items()
                },
            },
        ) from error
    return TrackDeleteResponse(deleted=deleted)


@router.get("/tracks/{track_name}/usage", response_model=TrackRunReferencePage)
async def get_track_usage(
    track_name: str,
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
) -> TrackRunReferencePage:
    try:
        return await assets_service.get_track_usage(track_name, page, page_size)
    except TrackNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidTrackError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/tracks/{track_name}/content")
async def get_track_content(
    track_name: str,
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
    download: bool = False,
) -> StreamingResponse:
    try:
        await assets_service.get_track(track_name)
        file = assets_service.open_track(track_name)
    except TrackNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidTrackError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    disposition = "attachment" if download else "inline"
    encoded_name = quote(track_name)
    return StreamingResponse(
        iter_file(file),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}"
        },
    )


@router.get("/tracks/{track_name}", response_model=TrackMetadata)
async def get_track(
    track_name: str,
    assets_service: Annotated[AssetsService, Depends(get_assets_service)],
) -> TrackMetadata:
    try:
        return await assets_service.get_track(track_name)
    except TrackNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidTrackError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
