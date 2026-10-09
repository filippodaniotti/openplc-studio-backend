"""Revision-keyed, study-scoped analyses and complete pseudonymous raw exports."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException

from plc_platform_backend.commons.base_mongodb_repository import BaseMongoDBRepository
from .participant_repository import ParticipantRepository
from .studies_models import ListeningStudyDocument, StudyStatus
from .study_statistics import ANALYSIS_VERSION, ANALYSIS_ENVIRONMENT, analyze_study


class AnalysisSnapshotsRepository(BaseMongoDBRepository):
    def __init__(self):
        super().__init__("listening_analysis_snapshots")

    async def get(self, key: str):
        record = await self.collection.find_one({"_id": key})
        return record["result"] if record is not None else None

    async def save(self, key: str, result: dict):
        # Live/filter caches are disposable. Canonical final results live in the
        # immutable study artifact directory, not a growing revision collection.
        await self.collection.create_index("expires_at", expireAfterSeconds=0)
        await self.collection.update_one({"_id": key}, {"$setOnInsert": {
            "result": result, "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        }}, upsert=True)


async def response_rows(study: ListeningStudyDocument, participants: ParticipantRepository) -> list[dict]:
    semaphore = asyncio.Semaphore(16)
    trials = {trial["id"]: trial for trial in study.definition["trials"]}
    async def session_rows(head):
        async with semaphore:
            checkpoint = await participants.checkpoint(head.checkpoint_id)
        if checkpoint is None:
            raise HTTPException(503, "A frozen/referenced participant checkpoint is missing")
        included = head.status == "completed" and not head.excluded
        return [
            {**row, "session_id": head.session_id, "session_status": head.status,
             "included": included, "excluded": head.excluded,
             "exclusion_reason": head.exclusion_reason,
             "track_id": trials[row["trial_id"]].get("excerpt", {}).get("track_id"),
             "excerpt_id": trials[row["trial_id"]].get("excerpt", {}).get("id"),
             "repetition": trials[row["trial_id"]].get("repetition"),
             "condition_role": next(condition.get("role") for condition in trials[row["trial_id"]]["conditions"] if condition["id"] == row["condition_id"]),
             "consent_version": checkpoint["payload"]["consent_version"],
             "consent_acknowledged": checkpoint["payload"]["consent_acknowledged"],
             "audio_context_sample_rate": checkpoint["payload"].get("audio_sample_rate")}
            for row in checkpoint["payload"]["responses"]
        ]
    batches = await asyncio.gather(*(session_rows(head) for _, head in sorted(study.sessions.items())))
    return [row for batch in batches for row in batch]


async def session_metadata(study: ListeningStudyDocument, participants: ParticipantRepository) -> list[dict]:
    """Export exact randomization/consent metadata once per pseudonymous session."""
    semaphore = asyncio.Semaphore(16)
    async def metadata(head):
        async with semaphore:
            checkpoint = await participants.checkpoint(head.checkpoint_id)
        if checkpoint is None:
            raise HTTPException(503, "A frozen/referenced participant checkpoint is missing")
        return {
            **head.model_dump(mode="json"),
            **{key: value for key, value in checkpoint["payload"].items() if key != "responses"},
            "checkpoint_created": checkpoint["created"].isoformat(),
            "included": head.status == "completed" and not head.excluded,
        }
    return list(await asyncio.gather(*(metadata(head) for _, head in sorted(study.sessions.items()))))


async def study_results(study: ListeningStudyDocument, participants: ParticipantRepository, snapshots: AnalysisSnapshotsRepository, **filters) -> dict:
    identity = {
        "study": study.id, "revision": study.revision, "version": ANALYSIS_VERSION,
        "environment": ANALYSIS_ENVIRONMENT,
        "heads": {key: head.model_dump(mode="json") for key, head in sorted(study.sessions.items())},
        "filters": filters,
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cached = await snapshots.get(key)
    if cached is not None:
        return cached
    rows = await response_rows(study, participants)
    included_rows = [row for row in rows if row["included"]]
    try:
        analysis = await asyncio.to_thread(analyze_study, study.definition, included_rows, **filters)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    heads = list(study.sessions.values())
    result = {
        "kind": "human-listening-study", "schema_version": 1,
        "study_id": study.id, "run_id": study.run_id, "study_revision": study.revision,
        "analysis_input_digest": key, "status": study.status,
        "provisional": study.status == StudyStatus.COLLECTING,
        "test_type": "mushra", "higher_is_better": True,
        "definition": study.definition, "conditions": study.conditions,
        "protocol": {"instructions": study.instructions, "consent": study.consent, "consent_version": study.consent_version},
        "counts": {
            "invited": len(heads), "draft": sum(head.status == "draft" for head in heads),
            "completed": sum(head.status == "completed" for head in heads),
            "excluded": sum(head.excluded for head in heads),
            "flagged": sum(bool(head.flags) for head in heads),
            "included": sum(head.status == "completed" and not head.excluded for head in heads),
        },
        "included_sessions": [head.session_id for head in heads if head.status == "completed" and not head.excluded],
        "quality_flags": {head.session_id: head.flags for head in heads if head.flags},
        "warnings": study.warnings, "analysis": analysis,
    }
    # Convert NumPy-compatible values and reject nonfinite JSON before caching.
    try:
        serializable = json.loads(json.dumps(result, allow_nan=False))
    except (ValueError, TypeError) as error:
        raise RuntimeError("Analysis produced an invalid JSON snapshot") from error
    await snapshots.save(key, serializable)
    return serializable
