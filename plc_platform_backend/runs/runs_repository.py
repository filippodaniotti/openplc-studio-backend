from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import re

from bson import ObjectId
from pymongo import ReturnDocument

from plc_platform_backend.commons.base_mongodb_repository import BaseMongoDBRepository
from plc_platform_backend.runs.runs_models import (
    Run,
    RunCreateDto,
    RunDocument,
    RunSortField,
    RunStatus,
    SortDirection,
)

COLLECTION_NAME = "runs"


@lru_cache
def get_runs_repository() -> RunsRepository:
    _module_service = RunsRepository()
    return _module_service


class RunsRepository(BaseMongoDBRepository):
    def __init__(self):
        super().__init__(COLLECTION_NAME)

    async def create_run(self, run: RunCreateDto) -> RunDocument:
        run_document = RunDocument(
            author=run.author,
            name=run.name,
            testbench_internal_id=run.testbench_internal_id,
            status=run.status,
            tracks=run.tracks,
            modules=run.modules,
        )

        run_document = await self.collection.insert_one(
            run_document.model_dump(by_alias=True, exclude={"id"})
        )

        created_run = await self.get_run(str(run_document.inserted_id))
        assert created_run is not None  # just inserted
        return created_run

    async def get_run(self, run_id: str) -> RunDocument | None:
        if not ObjectId.is_valid(run_id):
            return None

        run_data = await self.collection.find_one({"_id": ObjectId(run_id)})
        return RunDocument(**run_data) if run_data else None

    async def count_all(
        self,
        search: str | None = None,
        statuses: list[RunStatus] | None = None,
    ) -> int:
        return await self.collection.count_documents(
            self._build_page_filter(search, statuses)
        )

    async def get_page(
        self,
        skip: int,
        limit: int,
        search: str | None = None,
        statuses: list[RunStatus] | None = None,
        sort_by: RunSortField = "created",
        sort_direction: SortDirection = "desc",
    ) -> list[RunDocument]:
        direction = 1 if sort_direction == "asc" else -1
        runs_data = (
            await self.collection.find(self._build_page_filter(search, statuses))
            .sort([(sort_by, direction), ("_id", direction)])
            .skip(skip)
            .limit(limit)
            .to_list(length=limit)
        )
        return [RunDocument(**run_data) for run_data in runs_data]

    @staticmethod
    def _build_page_filter(
        search: str | None,
        statuses: list[RunStatus] | None,
    ) -> dict:
        query: dict = {}
        if search and search.strip():
            expression = re.escape(search.strip())
            query["$or"] = [
                {"name": {"$regex": expression, "$options": "i"}},
                {"author": {"$regex": expression, "$options": "i"}},
            ]
        if statuses:
            query["status"] = {"$in": [status.value for status in statuses]}
        return query

    async def get_runs_referencing_tracks(
        self, track_names: list[str]
    ) -> list[RunDocument]:
        if not track_names:
            return []
        runs_data = await self.collection.find(
            {"tracks": {"$in": track_names}}
        ).to_list(length=None)
        return [RunDocument(**run_data) for run_data in runs_data]

    async def count_runs_referencing_track(self, track_name: str) -> int:
        return await self.collection.count_documents({"tracks": track_name})

    async def get_runs_referencing_track(
        self, track_name: str, skip: int, limit: int
    ) -> list[RunDocument]:
        runs_data = (
            await self.collection.find({"tracks": track_name})
            .sort([("created", -1), ("_id", -1)])
            .skip(skip)
            .limit(limit)
            .to_list(length=limit)
        )
        return [RunDocument(**run_data) for run_data in runs_data]

    async def update_run(self, run_id: str, updated_run: Run) -> bool:
        updated_run.updated = datetime.utcnow()
        result = await self.collection.update_one(
            {"_id": ObjectId(run_id)},
            {
                "$set": updated_run.model_dump(
                    exclude={"id", "created"},
                )
            },
        )
        return result.modified_count > 0

    async def transition_status(
        self,
        run_id: str,
        expected_status: RunStatus,
        new_status: RunStatus,
    ) -> RunDocument | None:
        if not ObjectId.is_valid(run_id):
            return None

        run_data = await self.collection.find_one_and_update(
            {"_id": ObjectId(run_id), "status": expected_status.value},
            {"$set": {"status": new_status.value, "updated": datetime.utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return RunDocument(**run_data) if run_data else None

    async def delete_run(self, run_id: str) -> bool:
        if not ObjectId.is_valid(run_id):
            return False

        result = await self.collection.delete_one({"_id": ObjectId(run_id)})
        return result.deleted_count > 0
