"""Platform human-study cardinality constraints on creation and imports."""
import os
import asyncio
import pytest
from unittest.mock import MagicMock

os.environ.setdefault("MONGO_INITDB_ROOT_USERNAME", "test")
os.environ.setdefault("MONGO_INITDB_ROOT_PASSWORD", "test")
os.environ.setdefault("PLC_ROOT_FOLDER", "/tmp/plc-testbench-tests")
os.environ.setdefault("PLUGINS_DIRECTORY", "/tmp/plc-testbench-tests/plugins")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from plc_platform_backend.modules.modules_models import Module, ModuleType
from plc_platform_backend.runs.runs_models import RunConfigDto, RunCreateDto
from plc_platform_backend.runs.runs_service import RunsService


@pytest.mark.parametrize("humans,simulators,expected", [
    (0, 0, 0), (0, 2, 0), (1, 1, 0),
    (2, 1, 1), (1, 0, 1), (1, 2, 1), (2, 2, 2),
])
def test_human_run_limits(humans, simulators, expected):
    human = Module(name="HumanCalculator", settings=[])
    simulator = Module(name="BinomialPLS", settings=[])
    modules = {
        ModuleType.OutputAnalyser: [human] * humans,
        ModuleType.PacketLossSimulator: [simulator] * simulators,
    }
    manifest = MagicMock()
    manifest.get_all_modules_by_type.side_effect = lambda kind: {
        ModuleType.OutputAnalyser: [human],
        ModuleType.PacketLossSimulator: [simulator],
    }.get(kind, [])
    service = RunsService.__new__(RunsService)
    for dto, validate in [
        (RunConfigDto(name="test", tracks=[], modules=modules), service.validate_run_config),
        (RunCreateDto(author="test", name="test", tracks=[], modules=modules), service.validate_run_create),
    ]:
        errors = asyncio.run(validate(dto, manifest))
        limits = [error for error in errors if "instance" in error.error]
        assert len(limits) == expected
