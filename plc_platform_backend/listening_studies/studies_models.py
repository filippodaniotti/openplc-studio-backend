from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class StudyStatus(str, Enum):
    COLLECTING = "collecting"
    CLOSED = "closed"
    FINALIZED = "finalized"


class ListeningStudyView(BaseModel):
    id: str
    run_id: str
    human_module_index: int
    simulator_module_index: int
    status: StudyStatus
    revision: int = 0
    schema_version: int = 1
    created: datetime
    definition: dict
    conditions: list[dict]
    warnings: list[str] = Field(default_factory=list)
    trial_count: int
    instructions: str = "Rate audio quality from 0 (bad) to 100 (excellent). Use headphones in a quiet environment."
    consent: str = "I agree to participate in this listening study and to the collection of my pseudonymous ratings."
    consent_version: int = 1
    invitations_created: bool = False
    closed_at: datetime | None = None
    finalized_at: datetime | None = None


class StudyArtifactSource(BaseModel):
    node_id: str
    condition_id: str
    track_id: str
    source_path: str


class SessionHead(BaseModel):
    session_id: str
    checkpoint_id: str
    revision: int = 0
    status: str = "invited"
    revoked: bool = False
    excluded: bool = False
    exclusion_reason: str | None = None
    flags: list[str] = Field(default_factory=list)
    exclusion_audit: list[dict] = Field(default_factory=list)


class ListeningStudyDocument(ListeningStudyView):
    artifact_directory: str
    audio_files: list[str]
    sources: list[StudyArtifactSource]
    sessions: dict[str, SessionHead] = Field(default_factory=dict)
    finalized_directory: str | None = None
    finalized_outputs: dict[str, str] = Field(default_factory=dict)
