from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import BinaryIO, cast

from plctestbench.models import TestbenchConfiguration
from plctestbench.node import Node
from plctestbench.plc_testbench import PLCTestbench

from plc_platform_backend.assets.assets_models import (
    OriginalTrackMetadata,
    TestbenchNodeDepth,
    TrackMetadata,
)
from plc_platform_backend.assets.track_storage import TrackStorage, get_track_storage
from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.runs.runs_models import Run


@lru_cache
def get_assets_repository() -> AssetsRepository:
    return AssetsRepository()


class AssetsRepository:
    def __init__(self, track_storage: TrackStorage | None = None) -> None:
        self.track_storage = track_storage or get_track_storage()

    async def save_file(self, content: bytes, filename: str) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            yield content

        await self.track_storage.save(filename, chunks(), overwrite=True)

    async def save_track(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        overwrite: bool = False,
    ) -> TrackMetadata:
        return await self.track_storage.save(filename, chunks, overwrite=overwrite)

    async def delete_track(self, filename: str) -> None:
        await self.track_storage.delete(filename)

    async def get_track_metadata(self, filename: str) -> TrackMetadata:
        return await self.track_storage.get_metadata(filename)

    async def get_all_track_metadata(self) -> list[TrackMetadata]:
        return await self.track_storage.list_metadata()

    def open_track(self, filename: str) -> BinaryIO:
        return self.track_storage.open(filename)

    async def get_all_original_track_filenames(self) -> list[str]:
        return [track.name for track in await self.track_storage.list_metadata()]

    async def get_all_original_track_metadata(self) -> list[OriginalTrackMetadata]:
        return [
            OriginalTrackMetadata(
                name=track.name,
                size_bytes=track.size_bytes,
                duration_seconds=track.duration_seconds,
                sample_rate=track.sample_rate,
                channels=track.channels,
                bit_depth=track.bit_depth,
            )
            for track in await self.track_storage.list_metadata()
        ]

    def get_root_folder(self) -> pathlib.Path:
        return pathlib.Path(get_configuration().plc_root_folder).resolve()

    def get_original_track_basepath(self) -> pathlib.Path:
        return self.get_root_folder()

    def get_assets_paths(
        self,
        run: Run,
        depth: TestbenchNodeDepth,
        testbench_settings: TestbenchConfiguration,
    ) -> list[str]:
        testbench = PLCTestbench(
            run_id=cast(int, run.testbench_internal_id),
            testbench_settings=testbench_settings,
        )
        nodes: list[Node] = list(testbench.data_manager.get_nodes_by_depth(depth))
        return [file.get_path() for file in nodes]

    def resolve_asset_path(self, stem: str, depth: TestbenchNodeDepth) -> str:
        if depth == TestbenchNodeDepth.SAMPLE_MASKS:
            return f"{stem}.npy"
        if depth in (
            TestbenchNodeDepth.ORIGINAL_TRACKS,
            TestbenchNodeDepth.RECONSTRUCTED_TRACKS,
        ):
            return f"{stem}.wav"
        if depth == TestbenchNodeDepth.OUTPUT_ANALYSIS:
            return f"{stem}.pickle"
        raise ValueError(f"Unsupported asset depth: {depth}")
