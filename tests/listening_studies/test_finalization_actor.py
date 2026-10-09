import asyncio
import json
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.listening_studies.test_preparation import prepared_run
from plc_platform_backend.runs.runs_models import RunDocument, RunStatus


@pytest.mark.parametrize("outcome", ["success", "failure", "duplicate"])
def test_finalization_actor_claims_once_and_publishes_completion_only_after_finalization(tmp_path, monkeypatch, outcome):
    for key, value in {"MONGO_INITDB_ROOT_USERNAME": "test", "MONGO_INITDB_ROOT_PASSWORD": "test", "PLC_ROOT_FOLDER": str(tmp_path), "PLUGINS_DIRECTORY": str(tmp_path), "REDIS_URL": "redis://localhost:6379/0"}.items():
        monkeypatch.setenv(key, value)
    from plc_platform_backend import actors
    from plc_platform_backend.runs import runs_actors, runs_repository
    from plc_platform_backend.listening_studies import finalization

    run, _ = prepared_run(tmp_path)
    queued = RunDocument(**run.model_copy(update={"status": RunStatus.QUEUED}).model_dump())
    repository = SimpleNamespace(get_run=AsyncMock(return_value=queued), transition_status=AsyncMock(return_value=None if outcome == "duplicate" else queued))
    redis = SimpleNamespace(publish=AsyncMock())
    finalize = AsyncMock(side_effect=ValueError("injected storage failure") if outcome == "failure" else None)
    monkeypatch.setattr(runs_repository, "get_runs_repository", lambda: repository)
    monkeypatch.setattr(runs_actors, "get_redis_client", lambda: redis)
    monkeypatch.setattr(finalization, "finalize_studies", finalize)
    async def invoke():
        # Dramatiq wraps coroutine actors in its worker-thread synchronous adapter.
        # Exercise the original coroutine without starting a broker worker.
        work = inspect.unwrap(actors.finalize_listening_run.fn)(run.id)
        assert inspect.isawaitable(work)
        await work
    asyncio.run(invoke())
    if outcome == "duplicate":
        finalize.assert_not_called()
        redis.publish.assert_not_called()
        return
    finalize.assert_awaited_once_with(run.id)
    target = RunStatus.COMPLETED if outcome == "success" else RunStatus.FAILED
    assert [call.args[1:] for call in repository.transition_status.await_args_list] == [
        (RunStatus.QUEUED, RunStatus.RUNNING), (RunStatus.RUNNING, target),
    ]
    completion = [json.loads(call.args[1]) for call in redis.publish.await_args_list if json.loads(call.args[1])["type"] == "run.complete"]
    assert len(completion) == 1
    assert completion[0]["success"] is (outcome == "success")
