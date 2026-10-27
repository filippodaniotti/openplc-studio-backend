"""Immutable checkpoints with a single-document publication/closure barrier.

A checkpoint becomes visible only when its ID is atomically installed in the
collecting study's session head. Closing freezes those heads. Unpublished writes
cannot leak into a closed snapshot, even without MongoDB replica transactions.
"""
from functools import lru_cache
from datetime import datetime
from pymongo import ReturnDocument

from plc_platform_backend.commons.base_mongodb_repository import BaseMongoDBRepository
from plc_platform_backend.db.db_client import get_mongodb
from .studies_models import SessionHead


class ParticipantRepository(BaseMongoDBRepository):
    def __init__(self):
        super().__init__("listening_studies")
        database = get_mongodb().database
        self.invitations = database.get_collection("listening_invitations")
        self.checkpoints = database.get_collection("listening_checkpoints")

    async def invitation(self, digest: str):
        return await self.invitations.find_one({"_id": digest})

    async def checkpoint(self, checkpoint_id: str):
        return await self.checkpoints.find_one({"_id": checkpoint_id})

    async def stage_checkpoint(self, checkpoint: dict):
        await self.checkpoints.update_one(
            {"_id": checkpoint["_id"]}, {"$setOnInsert": checkpoint}, upsert=True,
        )
        stored = await self.checkpoint(checkpoint["_id"])
        if stored is None or stored["request_digest"] != checkpoint["request_digest"]:
            raise ValueError("Idempotency key was reused for a different checkpoint")
        return stored

    async def create_invitation(self, study_id: str, digest: str, head: SessionHead, consent_version: int):
        await self.invitations.insert_one({
            "_id": digest, "study_id": study_id, "session_id": head.session_id,
        })
        result = await self.collection.update_one(
            {"_id": study_id, "status": "collecting", "consent_version": consent_version,
             f"sessions.{head.session_id}": {"$exists": False},
             "$expr": {"$lt": [{"$size": {"$objectToArray": "$sessions"}}, 1000]}},
            {"$set": {f"sessions.{head.session_id}": head.model_dump()}, "$inc": {"revision": 1}},
        )
        return result.matched_count == 1

    async def publish_checkpoint(self, study_id: str, session_id: str, expected_revision: int, checkpoint_id: str, completed: bool, flags: list[str]):
        prefix = f"sessions.{session_id}"
        result = await self.collection.update_one(
            {"_id": study_id, "status": "collecting", f"{prefix}.revision": expected_revision,
             f"{prefix}.revoked": False, f"{prefix}.status": {"$ne": "completed"}},
            {"$set": {f"{prefix}.checkpoint_id": checkpoint_id,
                       f"{prefix}.status": "completed" if completed else "draft",
                       f"{prefix}.flags": flags},
             "$inc": {f"{prefix}.revision": 1, "revision": 1}},
        )
        return result.matched_count == 1

    async def update_instructions(self, study_id: str, revision: int, instructions: str, consent: str):
        result = await self.collection.update_one(
            {"_id": study_id, "status": "collecting", "revision": revision, "sessions": {}, "invitations_created": {"$ne": True}},
            {"$set": {"instructions": instructions, "consent": consent},
             "$inc": {"revision": 1, "consent_version": 1}},
        )
        return result.matched_count == 1

    async def revoke(self, study_id: str, session_id: str):
        result = await self.collection.update_one(
            {"_id": study_id, "status": "collecting", f"sessions.{session_id}": {"$exists": True}},
            {"$set": {f"sessions.{session_id}.revoked": True}, "$inc": {"revision": 1}},
        )
        return result.matched_count == 1

    async def delete_unused_invitation(self, study_id: str, session_id: str):
        """Remove an untouched head atomically against consent/save and closure.

        Removing the head invalidates every token before auxiliary cleanup. A
        staged concurrent checkpoint cannot publish after this barrier succeeds.
        Protocol editing remains locked even after deleting the last invitation.
        """
        prefix = f"sessions.{session_id}"
        removed = await self.collection.find_one_and_update(
            {"_id": study_id, "status": "collecting", f"{prefix}.revision": 0,
             f"{prefix}.status": "invited", f"{prefix}.excluded": False,
             f"{prefix}.exclusion_audit": []},
            {"$unset": {prefix: ""}, "$set": {"invitations_created": True},
             "$inc": {"revision": 1}},
            return_document=ReturnDocument.BEFORE,
        )
        if removed is None:
            return False
        await self.invitations.delete_many({"study_id": study_id, "session_id": session_id})
        await self.checkpoints.delete_many({"study_id": study_id, "session_id": session_id})
        return True

    async def exclude(self, study_id: str, session_id: str, revision: int, excluded: bool, reason: str, timestamp):
        prefix = f"sessions.{session_id}"
        result = await self.collection.update_one(
            {"_id": study_id, "status": "collecting", "revision": revision, prefix: {"$exists": True}},
            {"$set": {f"{prefix}.excluded": excluded, f"{prefix}.exclusion_reason": reason},
             "$push": {f"{prefix}.exclusion_audit": {"excluded": excluded, "reason": reason, "timestamp": timestamp}},
             "$inc": {"revision": 1}},
        )
        return result.matched_count == 1

    async def close(self, study_id: str, revision: int):
        return await self.collection.find_one_and_update(
            {"_id": study_id, "status": "collecting", "revision": revision},
            {"$set": {"status": "closed", "closed_at": datetime.utcnow()}, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )


@lru_cache
def get_participant_repository() -> ParticipantRepository:
    return ParticipantRepository()
