from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Literal, cast

from pydantic import BaseModel

from plc_platform_backend.commons.base_document import BaseDocument
from plc_platform_backend.modules.modules_models import Module, ModuleType


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    FAILED = "failed"
    COMPLETED = "completed"


class RunFailure(BaseModel):
    code: Literal["human_no_eligible_excerpts", "human_audio_budget_exceeded", "execution_failed"]
    message: str
    guidance: str


class RunDocument(BaseDocument):
    author: str
    name: str
    testbench_internal_id: Optional[str] = None
    status: RunStatus = RunStatus.CREATED
    failure: RunFailure | None = None
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]


class RunCreateDto(BaseModel):
    author: str
    name: str
    testbench_internal_id: Optional[str] = None
    status: RunStatus = RunStatus.CREATED
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]


class Run(BaseModel):
    id: str
    created: datetime
    updated: datetime
    author: str
    name: str
    testbench_internal_id: Optional[str]
    status: RunStatus = RunStatus.CREATED
    failure: RunFailure | None = None
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]

    @staticmethod
    def from_document(document: RunDocument) -> Run:
        return Run(
            id=cast(str, document.id),
            created=cast(datetime, document.created),
            updated=cast(datetime, document.updated),
            author=document.author,
            name=document.name,
            testbench_internal_id=document.testbench_internal_id,
            status=document.status,
            failure=document.failure,
            tracks=document.tracks,
            modules=document.modules,
        )


RunSortField = Literal["name", "created", "updated", "status"]
SortDirection = Literal["asc", "desc"]


class RunPage(BaseModel):
    items: list[Run]
    total: int
    page: int
    page_size: int


class RunDashboardCounts(BaseModel):
    running: int
    queued: int
    awaiting_input: int = 0
    completed_recent: int
    failed_recent: int


class RunDashboardSummary(BaseModel):
    generated_at: datetime
    recent_window_days: int
    counts: RunDashboardCounts
    active_runs: list[Run]
    recent_runs: list[Run]
    failed_runs: list[Run]


class NodeProgress(BaseModel):
    description: str
    node_id: str | None = None
    current: int
    total: int | None

    @property
    def percentage(self) -> float | None:
        if self.total and self.total > 0:
            return round((self.current / self.total) * 100, 1)
        return None


class RunCompletionMessage(BaseModel):
    type: Literal["run.complete"] = "run.complete"
    run_id: str
    run_name: str
    success: bool
    failure: RunFailure | None = None


class RunStateChangeMessage(BaseModel):
    type: Literal["run.state_change"] = "run.state_change"
    run_id: str
    run_name: str
    previous_status: RunStatus
    new_status: RunStatus


class RunProgressMessage(BaseModel):
    type: Literal["run.progress"] = "run.progress"
    run_id: str
    run_name: str
    nodes: list[NodeProgress]


# Model for RunConfigDto
class RunConfigDto(BaseModel):
    name: str
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]


# Model for RunConfigValidationError
class RunConfigValidationError(BaseModel):
    module_type: str
    module_name: str
    setting: str | None = None
    error: str
