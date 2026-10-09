import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.runs.test_runs_service import make_run_document
from plc_platform_backend.runs.runs_models import Run, RunStatus
from plc_platform_backend.runs.runs_service import _launch_run
from plc_platform_backend.modules.modules_models import Module, ModuleType
from plctestbench.human_listening import NoEligibleExcerptsError


def test_human_failure_is_persisted_atomically_and_published():
    document = make_run_document(RunStatus.RUNNING)
    document.modules[ModuleType.OutputAnalyser] = [Module(name='HumanCalculator', settings=[])]
    run = Run.from_document(document.model_copy(update={'status': RunStatus.QUEUED}))
    repository = SimpleNamespace(transition_status=AsyncMock(side_effect=[document, document.model_copy(update={'status': RunStatus.FAILED})]))
    redis = SimpleNamespace(publish=AsyncMock())
    service = SimpleNamespace(testbench_settings=SimpleNamespace(progress_monitor=None))
    testbench = SimpleNamespace(data_manager=SimpleNamespace(root_nodes=[]), run=MagicMock(side_effect=NoEligibleExcerptsError('private-track-id', 'Every excerpt contains multiple loss bursts.', 'Disable single_loss_per_stimulus or increase burst spacing.')))
    with patch('plc_platform_backend.runs.runs_service.PLCTestbench', return_value=testbench):
        asyncio.run(_launch_run(run, repository, service, redis))
    failure = repository.transition_status.await_args_list[1].kwargs['failure']
    assert failure.code == 'human_no_eligible_excerpts'
    payload = json.loads(redis.publish.await_args.args[1])
    assert payload['failure'] == failure.model_dump()
    assert 'private-track-id' not in json.dumps(payload)
    assert payload['success'] is False
