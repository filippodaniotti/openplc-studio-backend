from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Literal

from pydantic import BaseModel

from plc_platform_backend.commons.base_document import BaseDocument
from plc_platform_backend.modules.modules_models import Module, ModuleType


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


class RunDocument(BaseDocument):
    author: str
    name: str
    testbench_internal_id: str
    status: RunStatus = RunStatus.CREATED
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]


class RunCreateDto(BaseModel):
    author: str
    name: str
    testbench_internal_id: Optional[str]
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
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]

    @staticmethod
    def from_document(document: RunDocument) -> Run:
        return Run(
            id=document.id,
            created=document.created,
            updated=document.updated,
            author=document.author,
            name=document.name,
            testbench_internal_id=document.testbench_internal_id,
            status=document.status,
            tracks=document.tracks,
            modules=document.modules,
        )


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


class RunProgressMessage(BaseModel):
    type: Literal["run.progress"] = "run.progress"
    run_id: str
    run_name: str
    nodes: list[NodeProgress]

#Model for RunConfigDto
class RunConfigDto(BaseModel):
    name: str
    tracks: list[str]
    modules: dict[ModuleType, list[Module]]

#Model for RunConfigValidationError
class RunConfigValidationError(BaseModel):
    module_type: str
    module_name: str
    error: str

