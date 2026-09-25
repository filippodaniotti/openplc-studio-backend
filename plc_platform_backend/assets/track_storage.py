from __future__ import annotations

import os
import pathlib
import tempfile
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from functools import lru_cache
from typing import BinaryIO, Protocol

import soundfile as sf

from plc_platform_backend.assets.assets_models import StorageMetadata, TrackMetadata
from plc_platform_backend.commons.configuration.configuration import get_configuration

MAX_TRACK_SIZE_BYTES = 100_000_000
_CHUNK_SIZE = 1024 * 1024

_SUBTYPE_BIT_DEPTH: dict[str, int] = {
    "PCM_S8": 8,
    "PCM_U8": 8,
    "PCM_16": 16,
    "PCM_24": 24,
    "PCM_32": 32,
    "FLOAT": 32,
    "DOUBLE": 64,
}


class InvalidTrackError(ValueError):
    pass


class TrackAlreadyExistsError(FileExistsError):
    def __init__(self, filename: str) -> None:
        super().__init__(f"Track '{filename}' already exists")
        self.filename = filename


class TrackNotFoundError(FileNotFoundError):
    def __init__(self, filename: str) -> None:
        super().__init__(f"Track '{filename}' was not found")
        self.filename = filename


class TrackStorage(Protocol):
    async def list_metadata(self) -> list[TrackMetadata]:
        raise RuntimeError("TrackStorage protocol method called directly")

    async def get_metadata(self, filename: str) -> TrackMetadata:
        raise RuntimeError("TrackStorage protocol method called directly")

    async def save(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        overwrite: bool = False,
    ) -> TrackMetadata:
        raise RuntimeError("TrackStorage protocol method called directly")

    async def delete(self, filename: str) -> None:
        raise RuntimeError("TrackStorage protocol method called directly")

    def open(self, filename: str) -> BinaryIO:
        raise RuntimeError("TrackStorage protocol method called directly")


@lru_cache
def get_track_storage() -> TrackStorage:
    return LocalTrackStorage()


class LocalTrackStorage:
    provider = "filesystem"

    def __init__(self, root: pathlib.Path | None = None) -> None:
        configured_root = root or pathlib.Path(get_configuration().plc_root_folder)
        self.root = configured_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def list_metadata(self) -> list[TrackMetadata]:
        try:
            entries = sorted(self.root.iterdir(), key=lambda path: path.name.casefold())
        except OSError as error:
            raise OSError(f"Could not list tracks in '{self.root}': {error}") from error

        return [
            self._read_metadata(path)
            for path in entries
            if path.is_file() and path.suffix.lower() == ".wav"
        ]

    async def get_metadata(self, filename: str) -> TrackMetadata:
        path = self._path_for(filename)
        if not path.is_file():
            raise TrackNotFoundError(filename)
        return self._read_metadata(path)

    async def save(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        overwrite: bool = False,
    ) -> TrackMetadata:
        destination = self._path_for(filename)
        if destination.exists() and not overwrite:
            raise TrackAlreadyExistsError(filename)

        temporary_path: pathlib.Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.root, prefix=".track-upload-", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = pathlib.Path(temporary.name)
                size = 0
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_TRACK_SIZE_BYTES:
                        raise InvalidTrackError(
                            f"Track '{filename}' exceeds the 100 MB upload limit"
                        )
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

            if size == 0:
                raise InvalidTrackError(f"Track '{filename}' is empty")

            self._validate_audio(temporary_path, filename)
            os.replace(temporary_path, destination)
            temporary_path = None
            return self._read_metadata(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def delete(self, filename: str) -> None:
        path = self._path_for(filename)
        try:
            path.unlink()
        except FileNotFoundError as error:
            raise TrackNotFoundError(filename) from error
        except OSError as error:
            raise OSError(f"Could not delete track '{filename}': {error}") from error

    def open(self, filename: str) -> BinaryIO:
        path = self._path_for(filename)
        try:
            return path.open("rb")
        except FileNotFoundError as error:
            raise TrackNotFoundError(filename) from error

    def _path_for(self, filename: str) -> pathlib.Path:
        self._validate_filename(filename)
        return self.root / filename

    @staticmethod
    def _validate_filename(filename: str) -> None:
        if not filename or filename in {".", ".."}:
            raise InvalidTrackError("Track filename cannot be empty")
        if pathlib.PurePath(filename).name != filename or "/" in filename or "\\" in filename:
            raise InvalidTrackError("Track filename cannot contain directory components")
        if pathlib.Path(filename).suffix.lower() != ".wav":
            raise InvalidTrackError("Only WAV tracks are supported")

    @staticmethod
    def _validate_audio(path: pathlib.Path, filename: str) -> None:
        try:
            info = sf.info(path)
        except (RuntimeError, ValueError, OSError) as error:
            raise InvalidTrackError(f"Track '{filename}' is not a readable WAV file") from error
        if not str(info.format).upper().startswith("WAV"):
            raise InvalidTrackError(f"Track '{filename}' is not a WAV file")

    def _read_metadata(self, path: pathlib.Path) -> TrackMetadata:
        stat = path.stat()
        storage = StorageMetadata(
            provider=self.provider,
            key=path.name,
            content_type="audio/wav",
            size_bytes=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )
        try:
            info = sf.info(path)
        except (RuntimeError, ValueError, OSError):
            return TrackMetadata(name=path.name, size_bytes=stat.st_size, storage=storage)

        try:
            return TrackMetadata(
                name=path.name,
                size_bytes=stat.st_size,
                duration_seconds=float(info.duration),
                sample_rate=int(info.samplerate),
                channels=int(info.channels),
                bit_depth=_SUBTYPE_BIT_DEPTH.get(info.subtype),
                format=info.format,
                subtype=info.subtype,
                frames=int(info.frames),
                storage=storage,
            )
        except (TypeError, ValueError):
            return TrackMetadata(name=path.name, size_bytes=stat.st_size, storage=storage)


def iter_file(file: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := file.read(_CHUNK_SIZE):
            yield chunk
    finally:
        file.close()
