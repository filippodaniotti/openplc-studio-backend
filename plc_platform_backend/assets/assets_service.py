from __future__ import annotations

import io
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from functools import lru_cache
from tarfile import TarFile, TarInfo
from typing import BinaryIO

import numpy as np
from fastapi import UploadFile

from plc_platform_backend.assets.assets_models import (
    OriginalTrackMetadata,
    TrackMetadata,
    TrackPage,
    TrackRunReference,
    TrackRunReferencePage,
    TrackUsageSummary,
)
from plc_platform_backend.assets.assets_repository import (
    AssetsRepository,
    get_assets_repository,
)
from plc_platform_backend.runs.runs_models import RunDocument, RunStatus
from plc_platform_backend.runs.runs_repository import RunsRepository, get_runs_repository

_BLOCKING_STATUSES = {RunStatus.CREATED, RunStatus.QUEUED, RunStatus.RUNNING}
_UPLOAD_CHUNK_SIZE = 1024 * 1024


class TrackInUseError(RuntimeError):
    def __init__(self, references: dict[str, list[RunDocument]]) -> None:
        super().__init__("One or more tracks are referenced by active runs")
        self.references = references


@lru_cache
def get_assets_service() -> AssetsService:
    return AssetsService()


class AssetsService:
    def __init__(
        self,
        assets_repository: AssetsRepository | None = None,
        runs_repository: RunsRepository | None = None,
    ) -> None:
        self.assets_repository = assets_repository or get_assets_repository()
        self.runs_repository = runs_repository or get_runs_repository()

    async def save_file(self, data: bytes, filename: str) -> None:
        await self.assets_repository.save_file(data, filename)

    async def upload_track(
        self, file: UploadFile, overwrite: bool = False
    ) -> TrackMetadata:
        if not file.filename:
            raise ValueError("Uploaded track must have a filename")

        async def chunks() -> AsyncIterator[bytes]:
            while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
                yield chunk

        try:
            return await self.assets_repository.save_track(
                file.filename, chunks(), overwrite=overwrite
            )
        finally:
            await file.close()

    async def get_all_original_track_filenames(self) -> list[str]:
        return await self.assets_repository.get_all_original_track_filenames()

    async def get_all_original_track_metadata(self) -> list[OriginalTrackMetadata]:
        return await self.assets_repository.get_all_original_track_metadata()

    async def get_tracks_page(
        self,
        page: int,
        page_size: int,
        search: str | None = None,
        sort_by: str = "name",
        sort_direction: str = "asc",
    ) -> TrackPage:
        tracks = await self.assets_repository.get_all_track_metadata()
        if search:
            query = search.casefold().strip()
            tracks = [track for track in tracks if query in track.name.casefold()]

        usage_by_name = await self._usage_summaries([track.name for track in tracks])
        for track in tracks:
            track.usage = usage_by_name.get(track.name, TrackUsageSummary())

        self._sort_tracks(tracks, sort_by, sort_direction)
        total = len(tracks)
        start = (page - 1) * page_size
        return TrackPage(
            items=tracks[start : start + page_size],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_track(self, filename: str) -> TrackMetadata:
        track = await self.assets_repository.get_track_metadata(filename)
        summaries = await self._usage_summaries([filename])
        track.usage = summaries.get(filename, TrackUsageSummary())
        return track

    async def get_track_usage(
        self, filename: str, page: int, page_size: int
    ) -> TrackRunReferencePage:
        await self.assets_repository.get_track_metadata(filename)
        total = await self.runs_repository.count_runs_referencing_track(filename)
        documents = await self.runs_repository.get_runs_referencing_track(
            filename, (page - 1) * page_size, page_size
        )
        return TrackRunReferencePage(
            items=[self._to_reference(document) for document in documents],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def delete_tracks(self, filenames: list[str]) -> list[str]:
        names = list(dict.fromkeys(filenames))
        for name in names:
            await self.assets_repository.get_track_metadata(name)

        documents = await self.runs_repository.get_runs_referencing_tracks(names)
        blocking: dict[str, list[RunDocument]] = {}
        for document in documents:
            if document.status not in _BLOCKING_STATUSES:
                continue
            for name in names:
                if name in document.tracks:
                    blocking.setdefault(name, []).append(document)
        if blocking:
            raise TrackInUseError(blocking)

        for name in names:
            await self.assets_repository.delete_track(name)
        return names

    def open_track(self, filename: str) -> BinaryIO:
        return self.assets_repository.open_track(filename)

    async def _usage_summaries(
        self, filenames: list[str]
    ) -> dict[str, TrackUsageSummary]:
        summaries = {filename: TrackUsageSummary() for filename in filenames}
        documents = await self.runs_repository.get_runs_referencing_tracks(filenames)
        for document in documents:
            for filename in set(document.tracks).intersection(summaries):
                summary = summaries[filename]
                summary.total += 1
                summary.by_status[document.status] = (
                    summary.by_status.get(document.status, 0) + 1
                )
                if document.status in _BLOCKING_STATUSES:
                    summary.blocking += 1
        return summaries

    @staticmethod
    def _sort_tracks(
        tracks: list[TrackMetadata], sort_by: str, sort_direction: str
    ) -> None:
        getters = {
            "name": lambda track: track.name.casefold(),
            "size_bytes": lambda track: track.size_bytes,
            "duration_seconds": lambda track: track.duration_seconds,
            "last_modified": lambda track: track.storage.last_modified,
        }
        getter = getters.get(sort_by, getters["name"])
        tracks.sort(key=lambda track: getter(track) or 0, reverse=sort_direction == "desc")
        tracks.sort(key=lambda track: getter(track) is None)

    @staticmethod
    def _to_reference(document: RunDocument) -> TrackRunReference:
        return TrackRunReference(
            id=str(document.id),
            name=document.name,
            status=document.status,
            created=document.created or datetime.now(timezone.utc),
        )

    def add_json_to_tar(
        self, data: np.ndarray, tar: TarFile, original_path: str, original_ext: str
    ) -> TarFile:
        json_data = json.dumps(data.tolist())
        json_buffer = io.BytesIO(json_data.encode("utf-8"))
        tarinfo = TarInfo(
            name=os.path.basename(original_path).replace(original_ext, ".json")
        )
        tarinfo.size = len(json_data.encode("utf-8"))
        tar.addfile(tarinfo, json_buffer)
        return tar
