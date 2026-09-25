from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from plc_platform_backend.assets.assets_models import StorageMetadata, TrackMetadata
from plc_platform_backend.assets.assets_repository import AssetsRepository
from plc_platform_backend.assets.assets_service import AssetsService, TrackInUseError
from plc_platform_backend.modules.modules_models import ModuleType
from plc_platform_backend.runs.runs_models import RunDocument, RunStatus
from plc_platform_backend.runs.runs_repository import RunsRepository


def track(name: str) -> TrackMetadata:
    return TrackMetadata(
        name=name,
        size_bytes=1024,
        duration_seconds=1.0,
        sample_rate=48000,
        channels=2,
        bit_depth=16,
        storage=StorageMetadata(
            provider="filesystem",
            key=name,
            content_type="audio/wav",
            size_bytes=1024,
            last_modified=datetime(2026, 1, 1),
        ),
    )


def run_document(name: str, status: RunStatus, tracks: list[str]) -> RunDocument:
    return RunDocument(
        _id="507f1f77bcf86cd799439011",
        author="test",
        name=name,
        status=status,
        tracks=tracks,
        modules={
            ModuleType.PacketLossSimulator: [],
            ModuleType.PLCAlgorithm: [],
            ModuleType.OutputAnalyser: [],
        },
    )


class AssetsServiceTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.assets_repository = SimpleNamespace(
            get_all_track_metadata=AsyncMock(return_value=[track("z.wav"), track("a.wav")]),
            get_track_metadata=AsyncMock(side_effect=lambda name: track(name)),
            delete_track=AsyncMock(),
        )
        self.runs_repository = SimpleNamespace(
            get_runs_referencing_tracks=AsyncMock(return_value=[]),
        )
        self.service = AssetsService(
            cast(AssetsRepository, self.assets_repository),
            cast(RunsRepository, self.runs_repository),
        )

    async def test_track_page_sorts_filters_and_adds_usage(self) -> None:
        self.runs_repository.get_runs_referencing_tracks.return_value = [
            run_document("Active", RunStatus.RUNNING, ["a.wav"]),
            run_document("Finished", RunStatus.COMPLETED, ["a.wav"]),
        ]

        page = await self.service.get_tracks_page(1, 25, search="A.", sort_by="name")

        self.assertEqual([item.name for item in page.items], ["a.wav"])
        self.assertEqual(page.items[0].usage.total, 2)
        self.assertEqual(page.items[0].usage.blocking, 1)
        self.assertEqual(page.items[0].usage.by_status[RunStatus.RUNNING], 1)

    async def test_delete_is_blocked_by_created_queued_or_running_runs(self) -> None:
        self.runs_repository.get_runs_referencing_tracks.return_value = [
            run_document("Queued", RunStatus.QUEUED, ["a.wav"])
        ]

        with self.assertRaises(TrackInUseError):
            await self.service.delete_tracks(["a.wav", "z.wav"])

        self.assets_repository.delete_track.assert_not_awaited()

    async def test_delete_allows_historical_references(self) -> None:
        self.runs_repository.get_runs_referencing_tracks.return_value = [
            run_document("Finished", RunStatus.COMPLETED, ["a.wav"]),
            run_document("Failed", RunStatus.FAILED, ["z.wav"]),
        ]

        deleted = await self.service.delete_tracks(["a.wav", "z.wav", "a.wav"])

        self.assertEqual(deleted, ["a.wav", "z.wav"])
        self.assertEqual(
            self.assets_repository.delete_track.await_args_list,
            [(("a.wav",),), (("z.wav",),)],
        )
