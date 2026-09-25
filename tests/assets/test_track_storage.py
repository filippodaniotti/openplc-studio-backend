import io
import tempfile
import wave
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from plc_platform_backend.assets.track_storage import (
    InvalidTrackError,
    LocalTrackStorage,
    TrackAlreadyExistsError,
    TrackNotFoundError,
)


def wav_bytes(sample_rate: int = 8000, frames: int = 800) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


async def chunks(data: bytes):
    midpoint = len(data) // 2
    yield data[:midpoint]
    yield data[midpoint:]


class LocalTrackStorageTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.storage = LocalTrackStorage(self.root)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_saves_lists_and_reads_wav_metadata(self) -> None:
        metadata = await self.storage.save("track.wav", chunks(wav_bytes()))

        self.assertEqual(metadata.name, "track.wav")
        self.assertEqual(metadata.sample_rate, 8000)
        self.assertEqual(metadata.channels, 1)
        self.assertEqual(metadata.bit_depth, 16)
        self.assertEqual(metadata.frames, 800)
        self.assertEqual(metadata.storage.provider, "filesystem")
        self.assertEqual(metadata.storage.key, "track.wav")
        self.assertEqual([track.name for track in await self.storage.list_metadata()], ["track.wav"])

    async def test_rejects_conflicts_unless_overwrite_is_explicit(self) -> None:
        await self.storage.save("track.wav", chunks(wav_bytes(8000)))

        with self.assertRaises(TrackAlreadyExistsError):
            await self.storage.save("track.wav", chunks(wav_bytes(16000)))

        metadata = await self.storage.save("track.wav", chunks(wav_bytes(16000)), overwrite=True)
        self.assertEqual(metadata.sample_rate, 16000)

    async def test_rejects_invalid_names_and_content_without_leaving_files(self) -> None:
        with self.assertRaises(InvalidTrackError):
            await self.storage.save("../track.wav", chunks(wav_bytes()))
        with self.assertRaises(InvalidTrackError):
            await self.storage.save("track.mp3", chunks(wav_bytes()))
        with self.assertRaises(InvalidTrackError):
            await self.storage.save("broken.wav", chunks(b"not a wave"))

        self.assertEqual(list(self.root.glob("*.wav")), [])
        self.assertEqual(list(self.root.glob(".track-upload-*")), [])

    async def test_deletes_tracks_and_reports_missing_files(self) -> None:
        await self.storage.save("track.wav", chunks(wav_bytes()))
        await self.storage.delete("track.wav")

        with self.assertRaises(TrackNotFoundError):
            await self.storage.get_metadata("track.wav")
        with self.assertRaises(TrackNotFoundError):
            await self.storage.delete("track.wav")
