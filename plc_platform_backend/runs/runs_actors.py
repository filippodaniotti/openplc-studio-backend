import dramatiq

from plc_platform_backend.commons.redis_client import get_redis_client
from plc_platform_backend.runs import runs_repository, runs_service
from plc_platform_backend.runs.runs_models import Run, RunStatus


@dramatiq.actor()
async def launch_run(run_id: str) -> None:
    repository = runs_repository.get_runs_repository()
    service = runs_service.get_runs_service()
    redis_client = get_redis_client()

    run_document = await repository.get_run(run_id)
    if run_document is None:
        return

    await runs_service._launch_run(
        Run.from_document(run_document), repository, service, redis_client
    )


@dramatiq.actor(max_retries=0, time_limit=3600000)
async def finalize_listening_run(run_id: str) -> None:
    from plc_platform_backend.listening_studies.finalization import finalize_studies

    repository = runs_repository.get_runs_repository()
    redis = get_redis_client()
    document = await repository.get_run(run_id)
    if document is None:
        return
    running = await runs_service._transition_run_status(
        run_id, document.name, RunStatus.QUEUED, RunStatus.RUNNING, repository, redis,
    )
    if running is None:
        return
    try:
        await finalize_studies(run_id)
    except Exception:
        runs_service.logger.exception("Run %s human-result finalization failed", run_id)
        await runs_service._transition_run_status(
            run_id, document.name, RunStatus.RUNNING, RunStatus.FAILED, repository, redis,
        )
        await runs_service._publish_run_completion(run_id, document.name, False, redis)
        return
    completed = await runs_service._transition_run_status(
        run_id, document.name, RunStatus.RUNNING, RunStatus.COMPLETED, repository, redis,
    )
    if completed is not None:
        await runs_service._publish_run_completion(run_id, document.name, True, redis)
