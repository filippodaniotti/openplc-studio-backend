from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from plc_platform_backend.runs.runs_models import RunStatus
from plc_platform_backend.runs.runs_repository import RunsRepository


class RunsRepositoryInvalidIdTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = RunsRepository.__new__(RunsRepository)
        self.repository.collection = SimpleNamespace(
            find_one=AsyncMock(),
            find_one_and_update=AsyncMock(),
            delete_one=AsyncMock(),
        )

    async def test_invalid_id_is_not_found(self) -> None:
        self.assertIsNone(await self.repository.get_run("invalid"))
        self.repository.collection.find_one.assert_not_awaited()

    async def test_invalid_id_is_not_deleted(self) -> None:
        self.assertFalse(await self.repository.delete_run("invalid"))
        self.repository.collection.delete_one.assert_not_awaited()

    async def test_invalid_id_status_is_not_transitioned(self) -> None:
        self.assertIsNone(
            await self.repository.transition_status(
                "invalid", RunStatus.CREATED, RunStatus.QUEUED
            )
        )
        self.repository.collection.find_one_and_update.assert_not_awaited()

    def test_page_filter_escapes_search_and_filters_statuses(self) -> None:
        query = self.repository._build_page_filter(
            "run.*", [RunStatus.RUNNING, RunStatus.COMPLETED]
        )

        self.assertEqual(
            query,
            {
                "$or": [
                    {"name": {"$regex": r"run\.\*", "$options": "i"}},
                    {"author": {"$regex": r"run\.\*", "$options": "i"}},
                ],
                "status": {"$in": ["running", "completed"]},
            },
        )
