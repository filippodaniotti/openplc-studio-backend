from datetime import datetime, timezone
import hashlib
import json
import random
import secrets
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException

from .participant_models import CheckpointSave, ConsentAcknowledgement, InstructionsUpdate, SessionExclusion, StudyClose
from .audio_loading import AudioLoadingLimits
from .participant_repository import ParticipantRepository
from .studies_models import ListeningStudyDocument, SessionHead, StudyStatus
from .studies_repository import ListeningStudiesRepository


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _request_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def quality_flags(responses: list[dict], audio_sample_rate: int | None = None) -> list[str]:
    references = [row["score"] for row in responses if row["condition_id"] == "reference" and row["answered"]]
    flags = []
    if references and sum(score < 90 for score in references) / len(references) > 0.15:
        flags.append("hidden_reference_below_90_on_over_15_percent_of_trials")
    trial_scores = {(row["trial_id"], row["condition_id"]): row["score"] for row in responses if row["answered"]}
    if any(
        score >= trial_scores[(trial, "reference")]
        for (trial, condition), score in trial_scores.items()
        if condition == "anchor35" and (trial, "reference") in trial_scores
    ):
        flags.append("anchor_reference_ordering_anomaly")
    if audio_sample_rate is None:
        flags.append("audio_context_rate_unreported")
    elif audio_sample_rate != 48000:
        flags.append("audio_context_rate_differs_from_48000")
    return flags


def validate_ratings(study: ListeningStudyDocument, request: CheckpointSave) -> list[dict]:
    expected = {
        (trial["id"], condition["id"])
        for trial in study.definition["trials"] for condition in trial["conditions"]
    }
    if request.current_trial_id not in {trial["id"] for trial in study.definition["trials"]}:
        raise HTTPException(422, "Checkpoint position does not belong to this study")
    seen = set()
    answered = set()
    for row in request.responses:
        key = (row.trial_id, row.condition_id)
        if key not in expected or key in seen:
            raise HTTPException(422, "Unknown or duplicate trial/condition rating")
        seen.add(key)
        if row.answered:
            answered.add(key)
    if request.complete and answered != expected:
        raise HTTPException(422, "Every condition in every trial must be explicitly rated before completion")
    return [row.model_dump() for row in request.responses]


class ParticipantService:
    def __init__(self, studies: ListeningStudiesRepository, participants: ParticipantRepository):
        self.studies = studies
        self.participants = participants

    async def study(self, study_id: str) -> ListeningStudyDocument:
        study = await self.studies.get(study_id)
        if study is None:
            raise HTTPException(404, "Listening study not found")
        return study

    async def context(self, digest: str):
        invitation = await self.participants.invitation(digest)
        if invitation is None:
            raise HTTPException(403, "Invalid invitation")
        study = await self.study(invitation["study_id"])
        head = study.sessions.get(invitation["session_id"])
        if head is None or head.revoked:
            raise HTTPException(403, "Invitation has been revoked")
        checkpoint = await self.participants.checkpoint(head.checkpoint_id)
        if checkpoint is None:
            raise HTTPException(503, "Saved listening checkpoint is unavailable")
        return study, head, checkpoint

    async def create_invitations(self, study_id: str, count: int):
        if not 1 <= count <= 100:
            raise HTTPException(422, "Create between 1 and 100 invitations at a time")
        study = await self.study(study_id)
        if study.status != StudyStatus.COLLECTING:
            raise HTTPException(409, "Collection is closed")
        if len(study.sessions) + count > 1000:
            raise HTTPException(422, "Version 1 supports at most 1000 invitations per study")
        invitations = []
        for _ in range(count):
            session_id = str(uuid4())
            token = secrets.token_urlsafe(32)
            generator = random.Random(session_id)
            trials = [trial["id"] for trial in study.definition["trials"]]
            generator.shuffle(trials)
            orders = {}
            for trial in study.definition["trials"]:
                conditions = [condition["id"] for condition in trial["conditions"]]
                generator.shuffle(conditions)
                orders[trial["id"]] = conditions
            payload = {
                "consent_acknowledged": False, "consent_version": study.consent_version,
                "trial_order": trials, "condition_orders": orders,
                "current_trial_id": trials[0], "responses": [],
                "audio_sample_rate": None, "browser": None,
            }
            checkpoint_id = str(uuid4())
            await self.participants.stage_checkpoint({
                "_id": checkpoint_id, "study_id": study_id, "session_id": session_id,
                "revision": 0, "created": datetime.utcnow(), "payload": payload,
                "request_digest": _request_digest(payload),
            })
            if not await self.participants.create_invitation(study_id, token_digest(token), SessionHead(session_id=session_id, checkpoint_id=checkpoint_id), study.consent_version):
                raise HTTPException(409, "Collection closed, consent changed, or invitation limit reached")
            invitations.append({
                "invitation_id": session_id,
                "path": f"/listening-participant#invite={token}",
            })
        return invitations

    @staticmethod
    def public_checkpoint(study, head, checkpoint):
        return {
            "study_id": study.id, "study_status": study.status,
            "session_id": head.session_id, "status": head.status,
            "revision": head.revision, "definition": study.definition,
            "instructions": study.instructions, "consent": study.consent,
            **checkpoint["payload"],
            # Runtime policy, not immutable study content or participant input.
            # This also applies to existing invitations without rewriting audio.
            "audio_loading_limits": AudioLoadingLimits().public_limits(),
        }

    async def checkpoint(self, digest: str):
        study, head, checkpoint = await self.context(digest)
        from plc_platform_backend.runs.runs_repository import get_runs_repository

        run = await get_runs_repository().get_run(study.run_id)
        return {**self.public_checkpoint(study, head, checkpoint), "run_name": run.name if run else None}

    async def _save(self, digest: str, request, update: dict, completed: bool = False):
        study, head, current = await self.context(digest)
        checkpoint_id = str(uuid5(NAMESPACE_URL, f"listening-checkpoint:{head.session_id}:{request.request_id}"))
        request_digest = _request_digest(request.model_dump(mode="json", exclude_unset=True))
        if head.checkpoint_id == checkpoint_id:
            if current["request_digest"] != request_digest:
                raise HTTPException(409, "Idempotency key was reused with different input")
            return self.public_checkpoint(study, head, current)
        if study.status != StudyStatus.COLLECTING or head.status == "completed":
            raise HTTPException(409, "Session is read-only")
        if request.expected_revision != head.revision:
            raise HTTPException(409, "Checkpoint changed in another tab; reload before saving")
        payload = {**current["payload"], **update}
        rates = list(payload.get("audio_sample_rates", []))
        if payload["audio_sample_rate"] is not None and payload["audio_sample_rate"] not in rates:
            rates.append(payload["audio_sample_rate"])
        payload["audio_sample_rates"] = rates
        flagged_rate = next((rate for rate in rates if rate != 48000), payload["audio_sample_rate"])
        flags = quality_flags(payload["responses"], flagged_rate) if completed else []
        try:
            staged = await self.participants.stage_checkpoint({
                "_id": checkpoint_id, "study_id": study.id, "session_id": head.session_id,
                "revision": head.revision + 1, "created": datetime.utcnow(),
                "payload": payload, "request_digest": request_digest,
            })
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if not await self.participants.publish_checkpoint(study.id, head.session_id, head.revision, checkpoint_id, completed, flags):
            raise HTTPException(409, "Collection closed or checkpoint changed while saving")
        return self.public_checkpoint(
            study, head.model_copy(update={"revision": head.revision + 1, "checkpoint_id": checkpoint_id, "status": "completed" if completed else "draft", "flags": flags}), staged,
        )

    async def consent(self, digest: str, request: ConsentAcknowledgement):
        study, _, checkpoint = await self.context(digest)
        if not request.accepted or request.consent_version != study.consent_version:
            raise HTTPException(422, "Explicit consent to the current consent version is required")
        acknowledged_at = checkpoint["payload"].get("consent_acknowledged_at") if checkpoint["payload"]["consent_acknowledged"] else datetime.now(timezone.utc).isoformat()
        return await self._save(digest, request, {
            "consent_acknowledged": True, "consent_version": study.consent_version,
            "consent_acknowledged_at": acknowledged_at,
        })

    async def save(self, digest: str, request: CheckpointSave):
        study, _, current = await self.context(digest)
        if not current["payload"]["consent_acknowledged"]:
            raise HTTPException(403, "Consent must be acknowledged before rating")
        responses = validate_ratings(study, request)
        update = {"current_trial_id": request.current_trial_id, "responses": responses}
        for field in ("audio_sample_rate", "browser"):
            if field in request.model_fields_set:
                update[field] = getattr(request, field)
        return await self._save(digest, request, update, request.complete)

    async def update_instructions(self, study_id: str, request: InstructionsUpdate):
        from .protocol_html import sanitize_protocol_html

        try:
            instructions = sanitize_protocol_html(request.instructions)
            consent = sanitize_protocol_html(request.consent)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if not await self.participants.update_instructions(study_id, request.expected_revision, instructions, consent):
            raise HTTPException(409, "Instructions can change only before the first invitation; reload if the revision changed")

    async def exclude(self, study_id: str, session_id: str, request: SessionExclusion):
        if not request.reason.strip():
            raise HTTPException(422, "An exclusion/inclusion reason is required")
        if not await self.participants.exclude(study_id, session_id, request.expected_study_revision, request.excluded, request.reason.strip(), datetime.utcnow()):
            raise HTTPException(409, "Collection closed or changed; reload before excluding")

    async def close(self, study_id: str, request: StudyClose):
        study = await self.study(study_id)
        included = [head for head in study.sessions.values() if head.status == "completed" and not head.excluded]
        if not included and not request.confirm_empty:
            raise HTTPException(409, "No completed included sessions; explicit empty-results confirmation is required")
        closed = await self.participants.close(study_id, request.expected_revision)
        if closed is None:
            raise HTTPException(409, "Collection already closed or changed; reload before closing")
        return {"status": "closed", "included_sessions": len(included), "revision": closed["revision"]}
