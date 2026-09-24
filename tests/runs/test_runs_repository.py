from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from plc_platform_backend.runs.runs_repository import RunsRepository


class RunsRepositoryInvalidIdTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = RunsRepository.__new__(RunsRepository)
        self.repository.collection = SimpleNamespace(
            find_one=AsyncMock(),
            delete_one=AsyncMock(),
        )

    async def test_invalid_id_is_not_found(self) -> None:
        self.assertIsNone(await self.repository.get_run("invalid"))
        self.repository.collection.find_one.assert_not_awaited()

    async def test_invalid_id_is_not_deleted(self) -> None:
        self.assertFalse(await self.repository.delete_run("invalid"))
        self.repository.collection.delete_one.assert_not_awaited()
