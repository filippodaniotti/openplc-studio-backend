import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from plc_platform_backend.listening_studies.audio_loading import AudioLoadingBudgetError

from tests.runs.test_runs_service import make_run_document
from plc_platform_backend.runs.runs_models import Run, RunStatus
from plc_platform_backend.runs.runs_service import _launch_run
from plc_platform_backend.modules.modules_models import Module, ModuleType
from plctestbench.human_listening import NoEligibleExcerptsError


@pytest.mark.parametrize('error,code', [
    (NoEligibleExcerptsError('private-track-id', 'Every excerpt contains multiple loss bursts.', 'Disable single_loss_per_stimulus or increase burst spacing.'), 'human_no_eligible_excerpts'),
    (AudioLoadingBudgetError(1, 80 * 1024**2, 50, 4, 64 * 1024**2), 'human_audio_budget_exceeded'),
])
def test_human_failure_is_persisted_atomically_and_published(error, code):
    document = make_run_document(RunStatus.RUNNING)
    document.modules[ModuleType.OutputAnalyser] = [Module(name='HumanCalculator', settings=[])]
    run = Run.from_document(document.model_copy(update={'status': RunStatus.QUEUED}))
    repository = SimpleNamespace(transition_status=AsyncMock(side_effect=[document, document.model_copy(update={'status': RunStatus.FAILED})]))
    redis = SimpleNamespace(publish=AsyncMock())
    service = SimpleNamespace(testbench_settings=SimpleNamespace(progress_monitor=None))
    testbench = SimpleNamespace(data_manager=SimpleNamespace(root_nodes=[]), run=MagicMock(side_effect=error))
    with patch('plc_platform_backend.runs.runs_service.PLCTestbench', return_value=testbench):
        asyncio.run(_launch_run(run, repository, service, redis))
    failure = repository.transition_status.await_args_list[1].kwargs['failure']
    assert failure.code == code
    payload = json.loads(redis.publish.await_args.args[1])
    assert payload['failure'] == failure.model_dump()
    assert 'private-track-id' not in json.dumps(payload)
    assert payload['success'] is False
