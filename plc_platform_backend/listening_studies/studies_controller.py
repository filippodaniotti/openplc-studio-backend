"""Management reads under the platform's current trusted-deployment policy.

These are not participant-token routes. Owner authentication is intentionally
out of scope; deployments must protect management APIs until it is introduced.
"""
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID
import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from plc_platform_backend.runs.runs_repository import RunsRepository, get_runs_repository
from .studies_models import ListeningStudyDocument, ListeningStudyView
from .participant_models import InvitationCreate, InstructionsUpdate, SessionExclusion, StudyClose
from .results_service import AnalysisSnapshotsRepository, response_rows, session_metadata, study_results
from .finalization import RAW_COLUMNS, safe_csv_row, queue_finalization_if_ready
from .participant_controller import Service
from .studies_repository import ListeningStudiesRepository, get_listening_studies_repository

router = APIRouter(tags=["listening-studies"])
StudyRepository = Annotated[ListeningStudiesRepository, Depends(get_listening_studies_repository)]


async def _study_or_404(study_id: str, repository: ListeningStudiesRepository) -> ListeningStudyDocument:
    study = await repository.get(study_id)
    if study is None:
        raise HTTPException(404, "Listening study not found")
    return study


@router.get("/runs/{run_id}/listening-studies", response_model=list[ListeningStudyView])
async def list_studies(
    run_id: str, repository: StudyRepository,
    runs: Annotated[RunsRepository, Depends(get_runs_repository)],
):
    if await runs.get_run(run_id) is None:
        raise HTTPException(404, "Run not found")
    return await repository.for_run(run_id)


@router.get("/listening-studies/{study_id}", response_model=ListeningStudyView)
async def get_study(study_id: str, repository: StudyRepository):
    return await _study_or_404(study_id, repository)


@router.get("/listening-studies/{study_id}/config")
async def get_config(study_id: str, repository: StudyRepository):
    study = await _study_or_404(study_id, repository)
    directory = Path(study.artifact_directory).resolve()
    path = directory / "config.yaml"
    if path.resolve().parent != directory or not path.is_file():
        raise HTTPException(404, "Listening configuration not found")
    return FileResponse(path, media_type="application/yaml", filename="config.yaml")


@router.get("/listening-studies/{study_id}/audio/{filename}")
async def get_audio(study_id: str, filename: str, repository: StudyRepository):
    study = await _study_or_404(study_id, repository)
    if filename not in study.audio_files:
        raise HTTPException(404, "Listening audio not found")
    directory = (Path(study.artifact_directory) / "audio").resolve()
    path = directory / filename
    if path.resolve().parent != directory or not path.is_file():
        raise HTTPException(404, "Listening audio not found")
    return FileResponse(path, media_type="audio/wav")


@router.get("/listening-studies/{study_id}/sessions")
async def get_sessions(study_id: str, repository: StudyRepository):
    study = await _study_or_404(study_id, repository)
    return {"revision": study.revision, "status": study.status, "sessions": list(study.sessions.values())}


@router.post("/listening-studies/{study_id}/invitations", status_code=201)
async def create_invitations(study_id: str, request: InvitationCreate, service: Service):
    return await service.create_invitations(study_id, request.count)


@router.delete("/listening-studies/{study_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(study_id: str, invitation_id: UUID, service: Service):
    if not await service.participants.revoke(study_id, str(invitation_id)):
        raise HTTPException(409, "Invitation not found or collection closed")


@router.delete("/listening-studies/{study_id}/invitations/{invitation_id}/unused", status_code=204)
async def delete_unused_invitation(study_id: str, invitation_id: UUID, service: Service):
    if not await service.participants.delete_unused_invitation(study_id, str(invitation_id)):
        raise HTTPException(409, "Only untouched invitations without exclusion history can be deleted while collecting")


@router.put("/listening-studies/{study_id}/sessions/{session_id}/exclusion", status_code=204)
async def exclude_session(study_id: str, session_id: UUID, request: SessionExclusion, service: Service):
    await service.exclude(study_id, str(session_id), request)


@router.post("/listening-studies/{study_id}/close")
async def close_study(study_id: str, request: StudyClose, service: Service):
    result = await service.close(study_id, request)
    study = await service.study(study_id)
    try:
        result["finalization_queued"] = await queue_finalization_if_ready(study.run_id)
    except Exception as error:
        raise HTTPException(503, "Collection is frozen but finalization could not be queued; retry finalization") from error
    return result


@router.patch("/listening-studies/{study_id}/instructions", status_code=204)
async def update_instructions(study_id: str, request: InstructionsUpdate, service: Service):
    await service.update_instructions(study_id, request)


@router.post("/runs/{run_id}/listening-studies/finalize", status_code=202)
async def retry_finalization(run_id: str, runs: Annotated[RunsRepository, Depends(get_runs_repository)]):
    run = await runs.get_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    from plc_platform_backend.runs.runs_models import RunStatus
    try:
        queued = await queue_finalization_if_ready(run_id, retry=run.status == RunStatus.FAILED)
    except Exception as error:
        raise HTTPException(503, "Could not queue human-result finalization") from error
    if not queued:
        raise HTTPException(409, "Finalization requires every configured study closed and a waiting or failed run")
    return {"status": "queued"}


@router.get("/listening-studies/{study_id}/results")
async def get_results(
    study_id: str, service: Service,
    snapshots: Annotated[AnalysisSnapshotsRepository, Depends(AnalysisSnapshotsRepository)],
    track_id: Annotated[list[str] | None, Query()] = None,
    condition_id: Annotated[list[str] | None, Query()] = None,
    repetition: Annotated[list[int] | None, Query()] = None,
):
    study = await service.study(study_id)
    filters = {name: value for name, value in (("track_ids", track_id), ("condition_ids", condition_id), ("repetitions", repetition)) if value is not None}
    if study.status.value == "finalized" and not filters:
        if study.finalized_directory is None:
            raise HTTPException(503, "Frozen analysis artifact is unavailable")
        try:
            return json.loads((Path(study.finalized_directory) / "results.json").read_text())
        except (OSError, ValueError) as error:
            raise HTTPException(503, "Frozen analysis artifact is unreadable") from error
    return await study_results(study, service.participants, snapshots, **filters)


@router.get("/listening-studies/{study_id}/exports/sessions.json")
async def export_session_metadata(study_id: str, service: Service):
    study = await service.study(study_id)
    if study.finalized_directory is not None:
        path = Path(study.finalized_directory) / "sessions.json"
        if not path.is_file():
            raise HTTPException(503, "Frozen session metadata is unavailable")
        return FileResponse(path, media_type="application/json", filename=f"{study_id}-sessions.json")
    return await session_metadata(study, service.participants)


@router.get("/listening-studies/{study_id}/exports/responses.{format}")
async def export_responses(study_id: str, format: Literal["csv", "json"], service: Service):
    study = await service.study(study_id)
    media_type = "text/csv" if format == "csv" else "application/json"
    if study.finalized_directory is not None:
        path = Path(study.finalized_directory) / f"responses.{format}"
        if not path.is_file():
            raise HTTPException(503, "Frozen responses artifact is unavailable")
        return FileResponse(path, media_type=media_type, filename=f"{study_id}-responses.{format}")
    rows = await response_rows(study, service.participants)
    def stream():
        if format == "json":
            yield "["
            for index, row in enumerate(rows):
                yield ("," if index else "") + json.dumps(row, allow_nan=False)
            yield "]"
        else:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=RAW_COLUMNS)
            writer.writeheader()
            yield buffer.getvalue()
            for row in rows:
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerow(safe_csv_row(row))
                yield buffer.getvalue()
    return StreamingResponse(stream(), media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{study_id}-responses.{format}"',
        "Cache-Control": "no-store",
    })
