from functools import lru_cache
from datetime import datetime

from plc_platform_backend.commons.base_mongodb_repository import BaseMongoDBRepository
from .studies_models import ListeningStudyDocument


class ListeningStudiesRepository(BaseMongoDBRepository):
    def __init__(self):
        super().__init__("listening_studies")

    async def save_prepared(self, study: ListeningStudyDocument) -> None:
        # Study identity includes run, module-instance, simulator-instance/version.
        # Retries must never replace collection data or reset its revision/state.
        document = study.model_dump(mode="python")
        document["_id"] = document.pop("id")
        await self.collection.update_one(
            {"_id": study.id}, {"$setOnInsert": document}, upsert=True,
        )

    async def get(self, study_id: str) -> ListeningStudyDocument | None:
        document = await self.collection.find_one({"_id": study_id})
        if document is None:
            return None
        document["id"] = document.pop("_id")
        return ListeningStudyDocument(**document)

    async def mark_finalized(self, study_id: str, revision: int, directory: str, outputs: dict[str, str]) -> bool:
        result = await self.collection.update_one(
            {"_id": study_id, "status": "closed", "revision": revision},
            {"$set": {"status": "finalized", "finalized_at": datetime.utcnow(),
                       "finalized_directory": directory, "finalized_outputs": outputs},
             "$inc": {"revision": 1}},
        )
        return result.matched_count == 1

    async def for_run(self, run_id: str) -> list[ListeningStudyDocument]:
        documents = await self.collection.find({"run_id": run_id}).sort([
            ("human_module_index", 1), ("simulator_module_index", 1),
        ]).to_list(length=None)
        for document in documents:
            document["id"] = document.pop("_id")
        return [ListeningStudyDocument(**document) for document in documents]


@lru_cache
def get_listening_studies_repository() -> ListeningStudiesRepository:
    return ListeningStudiesRepository()
