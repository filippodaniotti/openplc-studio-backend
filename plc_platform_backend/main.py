import os
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import AsyncIterator, Callable, Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from plc_platform_backend.commons.configuration.configuration import get_configuration
from plc_platform_backend.db import get_mongodb
from plc_platform_backend.routers import assets, modules, runs, runs_ws


@asynccontextmanager
async def storage_setup(app: FastAPI) -> AsyncIterator[None]:
    print("storage lifespan")

    config = get_configuration()
    if not os.path.exists(config.plc_root_folder):
        os.makedirs(config.plc_root_folder)

    yield


@asynccontextmanager
async def db_setup(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    mongodb = get_mongodb()
    ping_response = await mongodb.database.command("ping")

    if int(ping_response["ok"]) != 1:
        raise Exception("Problem connecting to database cluster.")
    else:
        print("Connected to database cluster.")

    yield

    # Shutdown
    print("Shutting down db.")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(modules.router)
app.include_router(runs.router)
app.include_router(assets.router)
app.include_router(runs_ws.router)
