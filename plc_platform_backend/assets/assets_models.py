from datetime import datetime
from enum import IntEnum

from pydantic import BaseModel, Field

from plc_platform_backend.runs.runs_models import RunStatus


class TestbenchNodeDepth(IntEnum):
    ORIGINAL_TRACKS = 0
    SAMPLE_MASKS = 1
    RECONSTRUCTED_TRACKS = 2
    OUTPUT_ANALYSIS = 3


class OriginalTrackMetadata(BaseModel):
    name: str
    size_bytes: int
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bit_depth: int | None = None


class StorageMetadata(BaseModel):
    provider: str
    key: str
    content_type: str
    size_bytes: int
    last_modified: datetime


class TrackUsageSummary(BaseModel):
    total: int = 0
    blocking: int = 0
    by_status: dict[RunStatus, int] = Field(default_factory=dict)


class TrackMetadata(OriginalTrackMetadata):
    format: str | None = None
    subtype: str | None = None
    frames: int | None = None
    storage: StorageMetadata
    usage: TrackUsageSummary = Field(default_factory=TrackUsageSummary)


class TrackPage(BaseModel):
    items: list[TrackMetadata]
    total: int
    page: int
    page_size: int


class TrackRunReference(BaseModel):
    id: str
    name: str
    status: RunStatus
    created: datetime


class TrackRunReferencePage(BaseModel):
    items: list[TrackRunReference]
    total: int
    page: int
    page_size: int


class TrackDeleteRequest(BaseModel):
    names: list[str] = Field(min_length=1)


class TrackDeleteResponse(BaseModel):
    deleted: list[str]
