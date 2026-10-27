import logging
import os
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import AsyncIterator, Callable, Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.commons.logging_config import configure_logging
from plc_platform_backend.db import get_mongodb
from plc_platform_backend.documentation import mount_documentation
from plc_platform_backend.routers import assets, modules, plugins, runs, runs_ws
from plc_platform_backend.listening_studies.studies_controller import router as listening_studies_router
from plc_platform_backend.listening_studies.participant_controller import router as listening_participant_router
from plc_platform_backend.listening_studies.client_assets import mount_listening_client

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def storage_setup(app: FastAPI) -> AsyncIterator[None]:
    config = get_configuration()
    try:
        os.makedirs(config.plc_root_folder, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to prepare artifact directory: {config.plc_root_folder}"
        ) from exc

    logger.info("Artifact storage is ready at %s", config.plc_root_folder)
    yield


@asynccontextmanager
async def db_setup(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    mongodb = get_mongodb()
    ping_response = await mongodb.database.command("ping")

    if ping_response.get("ok") != 1:
        raise Exception("Problem connecting to database cluster.")

    logger.info("Connected to the MongoDB cluster")
    yield

    # Shutdown
    logger.info("Closing the MongoDB client")
    mongodb = get_mongodb()
    mongodb.client.close()


@asynccontextmanager
async def _manager(
    app: FastAPI,
    lifespans: Sequence[Callable[[FastAPI], AbstractAsyncContextManager[None]]],
) -> AsyncIterator[None]:
    exit_stack = AsyncExitStack()
    async with exit_stack:
        for lifespan in lifespans:
            await exit_stack.enter_async_context(lifespan(app))
        yield


class Lifespans:
    def __init__(
        self,
        lifespans: Sequence[Callable[[FastAPI], AbstractAsyncContextManager[None]]],
    ) -> None:
        self.lifespans = lifespans

    def __call__(self, app: FastAPI) -> AbstractAsyncContextManager[None]:
        self.app = app
        return _manager(app, lifespans=self.lifespans)


app = FastAPI(lifespan=Lifespans([db_setup, storage_setup]))
mount_documentation(app)
mount_listening_client(app)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200", "http://127.0.0.1:4200"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(modules.router)
app.include_router(plugins.router)
app.include_router(runs.router)
app.include_router(assets.router)
app.include_router(runs_ws.router)
app.include_router(listening_studies_router)
app.include_router(listening_participant_router)
