import os
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

os.environ.setdefault("MONGO_INITDB_ROOT_USERNAME", "test")
os.environ.setdefault("MONGO_INITDB_ROOT_PASSWORD", "test")
os.environ.setdefault("PLC_ROOT_FOLDER", "/tmp/plc-testbench-tests")
os.environ.setdefault("PLUGINS_DIRECTORY", "/tmp/plc-testbench-tests/plugins")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from plc_platform_backend.runs.runs_models import RunStatus
from plc_platform_backend.runs.runs_service import RunNotDeletableError, RunsService


class RunsServiceDeleteRunTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = RunsService.__new__(RunsService)
        self.service.runs_repository = SimpleNamespace(delete_run=AsyncMock())
        self.service.find_by_id = AsyncMock()

    async def test_deletes_completed_and_failed_runs(self) -> None:
        for status in (RunStatus.COMPLETED, RunStatus.FAILED):
            with self.subTest(status=status):
                self.service.find_by_id.return_value = SimpleNamespace(status=status)
                self.service.runs_repository.delete_run.return_value = True

                await self.service.delete_run("507f1f77bcf86cd799439011")

        self.assertEqual(self.service.runs_repository.delete_run.await_count, 2)

    async def test_rejects_created_and_running_runs(self) -> None:
        for status in (RunStatus.CREATED, RunStatus.RUNNING):
            with self.subTest(status=status):
                self.service.find_by_id.return_value = SimpleNamespace(status=status)

                with self.assertRaises(RunNotDeletableError):
                    await self.service.delete_run("507f1f77bcf86cd799439011")

        self.service.runs_repository.delete_run.assert_not_awaited()

    async def test_propagates_not_found_lookup(self) -> None:
        self.service.find_by_id.side_effect = ValueError("Run invalid not found")

        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.delete_run("invalid")

        self.service.runs_repository.delete_run.assert_not_awaited()

    async def test_treats_a_delete_race_as_not_found(self) -> None:
        self.service.find_by_id.return_value = SimpleNamespace(
            status=RunStatus.COMPLETED
        )
        self.service.runs_repository.delete_run.return_value = False

        with self.assertRaisesRegex(ValueError, "not found"):
            await self.service.delete_run("507f1f77bcf86cd799439011")
