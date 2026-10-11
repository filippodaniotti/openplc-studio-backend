from datetime import datetime
from enum import Enum

from pydantic import BaseModel

from plc_platform_backend.modules.modules_models import ModuleSpec, ModuleType


class PluginStatus(str, Enum):
    AVAILABLE = "available"
    INVALID = "invalid"


class PluginInventoryItem(BaseModel):
    filename: str
    status: PluginStatus
    module_type: ModuleType = ModuleType.PLCAlgorithm
    spec: ModuleSpec | None = None
    error: str | None = None


class PluginInventory(BaseModel):
    scanned_at: datetime
    items: list[PluginInventoryItem]


class PluginDiagnostic(BaseModel):
    message: str
    line: int | None = None
    column: int | None = None


class PluginValidation(BaseModel):
    valid: bool
    diagnostics: list[PluginDiagnostic]


class PluginSource(BaseModel):
    filename: str
    content: str
    revision: str
    plugin: PluginInventoryItem


class PluginSourceInput(BaseModel):
    content: str


class PluginSaveInput(PluginSourceInput):
    revision: str
