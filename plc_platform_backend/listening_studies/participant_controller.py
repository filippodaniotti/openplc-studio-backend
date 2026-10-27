"""Study/session-scoped participant cookies, isolated from management identity."""
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from .participant_models import CheckpointSave, ConsentAcknowledgement, InvitationExchange
from .participant_repository import get_participant_repository
from .participant_service import ParticipantService, token_digest
from .studies_repository import get_listening_studies_repository

router = APIRouter(prefix="/listening-participant", tags=["listening-participant"])
COOKIE_PREFIX = "plc_listening_session_"


@lru_cache
def get_participant_service() -> ParticipantService:
    return ParticipantService(get_listening_studies_repository(), get_participant_repository())


Service = Annotated[ParticipantService, Depends(get_participant_service)]


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    if not origin or urlsplit(origin).scheme != scheme or urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "A same-origin browser request is required")


async def authorized_context(request: Request, session_id: UUID, service: ParticipantService):
    # GET/audio fetches may omit Origin when same-origin. Reject cross-origin
    # credentialed reads too, independently of the deployment's CORS settings.
    if request.headers.get("origin") or request.headers.get("sec-fetch-site") in {"same-site", "cross-site"}:
        check_origin(request)
    token = request.cookies.get(COOKIE_PREFIX + str(session_id))
    if token is None:
        raise HTTPException(403, "A listening invitation is required")
    digest = token_digest(token)
    study, head, checkpoint = await service.context(digest)
    if head.session_id != str(session_id):
        raise HTTPException(403, "Invitation belongs to a different session")
    return digest, study, head, checkpoint


@router.post("/exchange")
async def exchange(request: Request, response: Response, invitation: InvitationExchange, service: Service):
    check_origin(request)
    checkpoint = await service.checkpoint(token_digest(invitation.token))
    session_id = checkpoint["session_id"]
    # Distinct names AND paths prevent another invitation/tab from changing the
    # identity of an already-open test. Browser traffic uses the /api gateway.
    response.set_cookie(
        COOKIE_PREFIX + session_id, invitation.token, httponly=True, secure=True,
        samesite="strict", path=f"/api/listening-participant/{session_id}",
        max_age=60 * 24 * 60 * 60,
    )
    response.headers["Cache-Control"] = "no-store"
    return checkpoint


@router.get("/{session_id}/checkpoint")
async def get_checkpoint(session_id: UUID, request: Request, response: Response, service: Service):
    digest, _, _, _ = await authorized_context(request, session_id, service)
    response.headers["Cache-Control"] = "no-store"
    return await service.checkpoint(digest)


@router.post("/{session_id}/consent")
async def consent(session_id: UUID, request: Request, acknowledgement: ConsentAcknowledgement, service: Service):
    check_origin(request)
    digest, _, _, _ = await authorized_context(request, session_id, service)
    return await service.consent(digest, acknowledgement)


@router.put("/{session_id}/checkpoint")
async def save_checkpoint(session_id: UUID, request: Request, checkpoint: CheckpointSave, service: Service):
    check_origin(request)
    digest, _, _, _ = await authorized_context(request, session_id, service)
    return await service.save(digest, checkpoint)


@router.get("/{session_id}/config")
async def config(session_id: UUID, request: Request, service: Service):
    _, study, _, _ = await authorized_context(request, session_id, service)
    path = Path(study.artifact_directory) / "config.yaml"
    if not path.is_file():
        raise HTTPException(503, "Published listening configuration is unavailable")
    return FileResponse(path, media_type="application/yaml", headers={"Cache-Control": "private, no-store"})


@router.get("/{session_id}/audio/{filename}")
async def audio(session_id: UUID, request: Request, filename: str, service: Service):
    _, study, _, checkpoint = await authorized_context(request, session_id, service)
    if not checkpoint["payload"]["consent_acknowledged"]:
        raise HTTPException(403, "Consent is required before playing study audio")
    if filename not in study.audio_files:
        raise HTTPException(404, "Audio not found")
    directory = (Path(study.artifact_directory) / "audio").resolve()
    path = directory / filename
    if path.resolve().parent != directory or not path.is_file():
        raise HTTPException(404, "Audio not found")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})
