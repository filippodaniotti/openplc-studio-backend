import dramatiq

from plc_platform_backend.commons.redis_client import get_redis_client
from plc_platform_backend.runs import runs_repository, runs_service


@dramatiq.actor()
async def launch_run(run_id: str) -> None:

    repository = runs_repository.get_runs_repository()
    service = runs_service.get_runs_service()
    redis_client = get_redis_client()

    run = await repository.get_run(run_id)

    await runs_service._launch_run(run, repository, service, redis_client)
