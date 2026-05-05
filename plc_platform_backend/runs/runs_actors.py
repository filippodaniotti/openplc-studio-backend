import dramatiq


from plc_platform_backend.runs import runs_repository, runs_service


@dramatiq.actor()
async def launch_run(run_id: str) -> None:

    repository = runs_repository.get_runs_repository()
    service = runs_service.get_runs_service()
   

    run = await repository.get_run(run_id)

    await runs_service._launch_run(run, repository, service)
